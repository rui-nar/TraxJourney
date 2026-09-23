"""Activity filtering logic for TraxJourney."""

from dataclasses import dataclass
from datetime import date
from typing import List, Optional, Set

from src.models.activity import Activity


@dataclass
class FilterCriteria:
    """Criteria for filtering activities.

    None on any field means no constraint on that dimension.
    For activity_types, None means all types pass; an empty set means nothing passes.
    """

    start_date: Optional[date] = None
    end_date: Optional[date] = None
    activity_types: Optional[Set[str]] = None  # None = all types

    def is_empty(self) -> bool:
        """Return True if no filters are active."""
        return (
            self.start_date is None
            and self.end_date is None
            and self.activity_types is None
        )


class FilterEngine:
    """Applies FilterCriteria to a list of Activity objects."""

    @staticmethod
    def apply(activities: List[Activity], criteria: FilterCriteria) -> List[Activity]:
        """Return activities matching all criteria.

        Date comparison uses .date() on start_date_local, which strips both
        the time-of-day component and any timezone info. This matches user
        intent (filter by calendar day) and avoids timezone edge cases.
        """
        if criteria is None or criteria.is_empty():
            return list(activities)

        lowercased_types: Optional[Set[str]] = (
            {t.lower() for t in criteria.activity_types}
            if criteria.activity_types is not None
            else None
        )

        result: List[Activity] = []
        for activity in activities:
            activity_date = activity.start_date_local.date()

            if criteria.start_date is not None and activity_date < criteria.start_date:
                continue
            if criteria.end_date is not None and activity_date > criteria.end_date:
                continue
            if lowercased_types is not None and activity.type.lower() not in lowercased_types:
                continue

            result.append(activity)

        return result

    @staticmethod
    def extract_activity_types(activities: List[Activity]) -> List[str]:
        """Return a sorted list of unique activity types present in the list."""
        return sorted({a.type for a in activities})
