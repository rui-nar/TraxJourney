"""REST project item-ordering endpoints — delete/reorder/sort timeline items.

Routes:
    DELETE /api/projects/{name}/items/{index}   — remove item at index
    PUT    /api/projects/{name}/items/reorder   — move item from/to index
    PUT    /api/projects/{name}/items/sort      — sort items chronologically
"""
from __future__ import annotations

from typing import Annotated, Dict, List

from models.db import get_session

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel

from api.deps import get_current_user
from api.geo import bust_geo_cache
from api.project_access import OwnerParam, journal_visible_positions, resolve_project
from api.project_shared import _refresh_share_tiles, _refresh_stats_background, _repo, queue_share_tiles_refresh, queue_stats_refresh
from src.project.project_io import ProjectIO

router = APIRouter(prefix="/api/projects", tags=["projects"])


# ── Item management (delete + reorder) ────────────────────────────────────────

@router.delete("/{name}/items/{index}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Remove an item from the project")
def delete_item(
    name: str,
    index: int,
    current_user: Annotated[dict, Depends(get_current_user)],
    background_tasks: BackgroundTasks,
    owner: OwnerParam = None,
):
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner, min_role="editor")
        owner_id = row.user_info_id
        project_row_id = row.id

    removed: Dict[str, object] = {}

    def _remove(project):
        # The client's index points into its *visible* item list — other users'
        # journal items are hidden from it (issue #106). Translate to the full
        # list. Recomputed per attempt: a retry runs against a reloaded project
        # whose item list may have shifted under us.
        visible = journal_visible_positions(project.items, user_info_id, owner_id)
        if index < 0 or index >= len(visible):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Index out of range")
        real_index = visible[index]
        removed["item"] = project.items[real_index]
        project.remove_item(real_index)

    project = _repo.save_project_with_retry(
        owner_id, name, _remove)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    # A split tail is a LOCAL (negative-id) activity owned solely by its
    # timeline item. remove_item only unlinks the item, so without this the
    # row is orphaned in the activity table and its negative id gets reused
    # by the next split → UNIQUE constraint failure. Delete the row once no
    # remaining item references it.
    gone = removed["item"]
    if (
        gone.item_type == "activity"
        and (gone.activity_id or 0) < 0
        and not any(
            it.item_type == "activity" and it.activity_id == gone.activity_id
            for it in project.items
        )
    ):
        with get_session() as sess:
            _repo.delete_local_activity(sess, project_row_id, gone.activity_id)
    bust_geo_cache(owner_id, name)
    queue_stats_refresh(background_tasks, owner_id, name)
    queue_share_tiles_refresh(background_tasks, owner_id, name)


class ReorderRequest(BaseModel):
    from_index: int
    to_index: int


@router.put("/{name}/items/reorder", summary="Reorder project items")
def reorder_items(
    name: str,
    body: ReorderRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    background_tasks: BackgroundTasks,
    owner: OwnerParam = None,
):
    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner, min_role="editor")
        owner_id = row.user_info_id

    def _move(project):
        # from/to are indices into the caller's *visible* item list (issue #106):
        # translate before moving, and answer with the visible list only.
        visible = journal_visible_positions(project.items, user_info_id, owner_id)
        if 0 <= body.from_index < len(visible):
            to_index = max(0, min(len(visible) - 1, body.to_index))
            project.move_item(visible[body.from_index], visible[to_index])

    project = _repo.save_project_with_retry(
        owner_id, name, _move)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    bust_geo_cache(owner_id, name)
    queue_stats_refresh(background_tasks, owner_id, name)
    queue_share_tiles_refresh(background_tasks, owner_id, name)
    visible = journal_visible_positions(project.items, user_info_id, owner_id)
    return [ProjectIO._serialise_item(project.items[i]) for i in visible]


@router.put("/{name}/items/sort", status_code=status.HTTP_204_NO_CONTENT,
            summary="Sort project items chronologically")
def sort_items(
    name: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    background_tasks: BackgroundTasks,
    owner: OwnerParam = None,
) -> None:
    """Re-order all project items by date/time.

    Sort keys by item type:
    - activity  → start_date_local
    - memory    → date + time
    - journal   → date + time
    - segment   → date field if set; otherwise inherits the date of the
                  preceding dated item so undated segments stay near the
                  activities they connect.
    Items with no resolvable date are placed at the end, preserving their
    relative order (stable sort).
    """
    FALLBACK = "9999-12-31T23:59:59"

    user_info_id = int(current_user["sub"])
    with get_session() as sess:
        row = resolve_project(sess, user_info_id, name, owner, min_role="editor")
        owner_id = row.user_info_id

    def _sort(project):
        # Build a lookup: (lat_4dp, lon_4dp) → activity start_date isoformat
        # for every activity end-point. Segments whose start coordinates match
        # an activity's end coordinates are sorted immediately after that activity,
        # regardless of where they currently sit in the list.
        act_end_to_date: Dict[tuple, str] = {}
        for item in project.items:
            if item.item_type == "activity" and item.activity_id is not None:
                act = project._activity_map.get(item.activity_id)
                if act and act.end_latlng and act.start_date:
                    k = (round(act.end_latlng[0], 4), round(act.end_latlng[1], 4))
                    act_end_to_date[k] = act.start_date.isoformat()

        # First pass: assign each item a sort key.
        keys: List[str] = []
        last_date = FALLBACK
        for item in project.items:
            key: str = FALLBACK
            if item.item_type == "activity" and item.activity_id is not None:
                act = project._activity_map.get(item.activity_id)
                if act and act.start_date:
                    key = act.start_date.isoformat()
            elif item.item_type == "memory" and item.memory is not None:
                d = item.memory.date
                t = item.memory.time or "00:00"
                if d:
                    key = f"{d}T{t}"
            elif item.item_type == "journal" and item.journal is not None:
                d = item.journal.date
                t = getattr(item.journal, "time", None) or "00:00"
                if d:
                    key = f"{d}T{t}"
            elif item.item_type == "encounter" and item.encounter is not None:
                d = item.encounter.date
                t = getattr(item.encounter, "time", None) or "00:00"
                if d:
                    key = f"{d}T{t}"
            elif item.item_type == "segment" and item.segment is not None:
                seg = item.segment
                # Primary: match segment start → activity end by coordinates.
                if seg.start:
                    coord_key = (round(seg.start.lat, 4), round(seg.start.lon, 4))
                    matched = act_end_to_date.get(coord_key)
                    if matched:
                        key = matched  # sort right after the departing activity
                # Fallback: use date field or inherit from predecessor.
                if key == FALLBACK:
                    if seg.date:
                        pred_day = last_date[:10] if last_date != FALLBACK else None
                        key = last_date if pred_day == seg.date else f"{seg.date}T00:00:01"
                    else:
                        key = last_date

            if key != FALLBACK:
                last_date = key
            keys.append(key)

        # Stable sort: items with the same key preserve their original order.
        project.items = [
            item for _, item in sorted(
                zip(keys, project.items), key=lambda t: t[0]
            )
        ]

    if _repo.save_project_with_retry(
            owner_id, name, _sort) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    bust_geo_cache(owner_id, name)
    queue_stats_refresh(background_tasks, owner_id, name)
    queue_share_tiles_refresh(background_tasks, owner_id, name)
