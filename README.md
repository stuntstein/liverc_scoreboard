# LiveRC Series Score

Generates HTML scoreboards from LiveRC race results for RC car racing series.

## Quick Start

```bash
python3 build_scoreboard.py spring_series.json     # single series
python3 build_scoreboard.py --all                  # all .json configs in directory
```

## Project Structure

- `build_scoreboard.py` — Main script. Fetches/parses LiveRC HTML, computes scores, generates static HTML scoreboard.
- `*.json` — Series config files (one per series).
- `debug/` — Auto-saved HTML from fetched pages (when `DEBUG_SAVE = True`).
- `racers_den_spring_series/` — Legacy locally-saved HTML result files.

## Config Format

```json
{
    "track": "Track Name",
    "title": "Series Name Scoreboard",
    "max_points": 100,
    "points_gap": 3,
    "pole_position_points": 2,
    "series_length": 4,
    "races": [
        "https://theracersden.liverc.com/results/?p=view_heat_sheet&id=XXXXX",
        "https://theracersden.liverc.com/results/?p=view_heat_sheet&id=YYYYY"
    ]
}
```

- `track` — Track name shown above the title (optional).

- `races` — List of URLs (heat sheet pages) or local file paths. Columns auto-name as "Race 1", "Race 2", etc.
- `series_length` — Total races in the series. Unfinished races show as empty columns.
- `max_points` — Points for 1st place (default: 100).
- `points_gap` — Points gap between 1st and 2nd place (default: 1). Subsequent positions decrease by 1. E.g. gap=3 means 1st=100, 2nd=97, 3rd=96, 4th=95, ...
- `pole_position_points` — Bonus points for pole position (first qualifier). Set 0 to disable.

The output HTML is written next to the config with the same basename — `spring_series.json` → `spring_series.html`.

## How It Works

1. **Heat sheet parsing** (`HeatSheetParser`): Parses `view_heat_sheet` pages for class names, qualifying order (pole = first driver), and `view_race_result` links.
2. **Result parsing** (`RaceResultParser`): Follows each result link, extracts finish positions from `<span class="driver_name">` elements.
3. **Legacy support** (`LiveRCParser`): Parses saved "Overall Final Ranking" HTML files (auto-detected).
4. **Scoring**: Position points + optional pole bonus. Sorting by drop-worst total, then raw total, then races entered.
5. **HTML generation**: Static HTML with embedded JSON. JavaScript renders tables client-side with per-class result links, pole markers (gold "P"), and green/yellow race status columns.

## Architecture Notes

- No external dependencies — stdlib only (`html.parser`, `urllib`, `json`).
- URL fetching uses `User-Agent: Mozilla/5.0` header.
- `fetch_html()` handles both URLs and local files transparently.
- Auto-detection via `is_heat_sheet()` checks URL pattern or HTML content.
- All config values flow as parameters (no globals) — `cfg` dict passed through the call chain.

## Examples
https://racersden.stuntstein.dk/spring_offroad_series.html
