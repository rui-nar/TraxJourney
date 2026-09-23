# traxjourney_client

Flutter client (web, Android, iOS) for TraxJourney.

## Prerequisites

- Flutter SDK ≥ 3.0
- Dart SDK ≥ 3.0
- Backend running at `http://localhost:8000` (see root README)

## Running

```bash
flutter pub get
flutter run -d chrome --web-port 5500
```

The app connects to the backend at `http://localhost:8000`. To change this, edit `lib/src/api/client.dart`.

## Building for Production

```bash
flutter build web --release
```

Output in `build/web/` — serve as static files behind a web server or CDN.

## Screens

| Route | Screen |
|---|---|
| `/login` | Google OAuth login |
| `/register` | Email/password registration |
| `/projects` | Project picker + Strava connect card |
| `/app?project={name}` | Main project screen (map + activity panel) |
| `/strava-import?project={name}` | Strava activity browser + import |

## Key Packages

| Package | Purpose |
|---|---|
| `flutter_map` | Interactive map (OpenStreetMap) |
| `flutter_map_cancellable_tile_provider` | Cancel in-flight tile requests |
| `fl_chart` | Elevation profile chart |
| `go_router` | Navigation + auth guard |
| `provider` | State management |
| `url_launcher` | Open Strava OAuth URL in browser |
