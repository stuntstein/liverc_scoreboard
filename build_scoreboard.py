#!/usr/bin/env python3
"""
Parses LiveRC HTML result pages and generates a series scoreboard.

Supports two input modes:
  1. Heat sheet URL/file (view_heat_sheet) - parses qualifying order + follows result links
  2. Overall ranking file (legacy) - parses saved overall ranking HTML

Usage:
    python3 build_scoreboard.py <config.json>
    python3 build_scoreboard.py --all           # process all .json files in current directory

Config file format (JSON):
    {
        "title": "My Series Scoreboard",
        "max_points": 20,
        "pole_position_points": 1,
        "series_length": 4,
        "races": [
            {"name": "R1", "source": "https://...view_heat_sheet&id=XXXXX"},
            {"name": "R2", "source": "path/to/local_file.html"}
        ]
    }
"""

import re
import os
import sys
import json
from html.parser import HTMLParser
from urllib.request import urlopen, Request
from urllib.parse import urljoin

# ============================================================
# CONFIG LOADING
# ============================================================

def load_config(config_path):
    """Load series configuration from a JSON file."""
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    return {
        "title": cfg["title"],
        "track": cfg.get("track"),
        "logo": cfg.get("logo"),
        "max_points": cfg.get("max_points", 100),
        "points_gap": cfg.get("points_gap", 1),
        "pole_position_points": cfg.get("pole_position_points", 0),
        "series_length": cfg.get("series_length", len(cfg["races"])),
        "races": cfg["races"],
    }

# ============================================================
# URL / FILE FETCHING
# ============================================================

DEBUG_SAVE = False  # Save fetched HTML to debug/ directory for inspection

def fetch_html(source):
    """Fetch HTML from a URL or read from a local file. Returns HTML string."""
    if source.startswith("http"):
        req = Request(source, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req) as resp:
            html = resp.read().decode("utf-8")
        if DEBUG_SAVE:
            import hashlib
            debug_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug")
            os.makedirs(debug_dir, exist_ok=True)
            fname = hashlib.md5(source.encode()).hexdigest()[:10] + ".html"
            with open(os.path.join(debug_dir, fname), "w", encoding="utf-8") as f:
                f.write(html)
            print(f"    [debug] Saved to debug/{fname}")
        return html
    else:
        with open(source, "r", encoding="utf-8") as f:
            return f.read()

# ============================================================
# HEAT SHEET PARSER (qualifying page)
# ============================================================

class HeatSheetParser(HTMLParser):
    """Parses a LiveRC heat sheet page to extract classes, qualifying order, and result links.

    Expected HTML structure:
      <span class="class_header">CLASS_NAME A-Main</span>
      <span class="car_num">1</span>DRIVER NAME
      <span class="race_status"><a href="/results/?p=view_race_result&id=XXXXX">View Results</a></span>
    """

    def __init__(self):
        super().__init__()
        self.classes = []  # [(class_name, [driver, ...], result_link), ...]
        self.event_date = None
        self._current_class = None
        self._current_drivers = []
        self._current_link = None
        self._in_class_header = False
        self._in_car_num = False
        self._in_race_status = False
        self._in_date_h5 = False
        self._expect_driver = False
        self._buf = ""

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        cls = attrs_dict.get("class", "")

        if tag == "span" and cls == "class_header":
            self._finalize_class()
            self._in_class_header = True
            self._buf = ""
        elif tag == "span" and cls == "car_num":
            self._in_car_num = True
            self._expect_driver = False
            self._buf = ""
        elif tag == "span" and cls == "race_status":
            self._in_race_status = True
            self._expect_driver = False
        elif tag == "a" and self._in_race_status:
            href = attrs_dict.get("href", "")
            if "view_race_result" in href:
                self._current_link = href
        elif tag == "h5" and "page-header" in cls and not self.event_date:
            self._in_date_h5 = True
            self._buf = ""
        elif tag in ("span", "div", "a", "br"):
            self._expect_driver = False

    def handle_endtag(self, tag):
        if tag == "span" and self._in_class_header:
            self._in_class_header = False
            name = self._buf.strip()
            for suffix in (" A-Main", " A Main", " A-main", " a-main"):
                if name.endswith(suffix):
                    name = name[:-len(suffix)]
                    break
            self._current_class = name
            self._current_drivers = []
            self._current_link = None
        elif tag == "span" and self._in_car_num:
            self._in_car_num = False
            self._expect_driver = True
        elif tag == "span" and self._in_race_status:
            self._in_race_status = False
        elif tag == "h5" and self._in_date_h5:
            self._in_date_h5 = False
            date_text = self._buf.strip()
            if date_text:
                self.event_date = date_text

    def handle_data(self, data):
        if self._in_class_header:
            self._buf += data
        elif self._in_date_h5:
            self._buf += data
        elif self._in_car_num:
            pass
        elif self._expect_driver and self._current_class is not None:
            stripped = data.strip()
            if stripped:
                self._current_drivers.append(stripped)
                self._expect_driver = False
        # ignore all other text

    def _finalize_class(self):
        if self._current_class and self._current_drivers:
            self.classes.append((self._current_class, list(self._current_drivers), self._current_link))
        self._current_class = None
        self._current_drivers = []
        self._current_link = None

    def close(self):
        self._finalize_class()
        super().close()

# ============================================================
# RACE RESULT PARSER (individual race result page)
# ============================================================

class RaceResultParser(HTMLParser):
    """Parses a LiveRC race result page (view_race_result).

    Expected HTML structure:
      <tbody>
        <tr>
          <td>1</td>
          <td>
            <span class="car_num">N</span>
            <span class="driver_name">DRIVER NAME</span>
            <br/><a class="driver_laps">View Laps</a>
          </td>
          <td>qual_pos</td>
          <td>result</td>
          ...
        </tr>
      </tbody>
    """

    def __init__(self):
        super().__init__()
        self.results = []  # [(pos, driver), ...]
        self._in_tbody = False
        self._in_td = False
        self._in_driver_name = False
        self._row_pos = None      # first td text = finish position
        self._row_driver = None   # driver_name span text
        self._current_cell = ""
        self._td_count = 0

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "tbody":
            self._in_tbody = True
        elif tag == "tr" and self._in_tbody:
            self._row_pos = None
            self._row_driver = None
            self._td_count = 0
        elif tag == "td" and self._in_tbody:
            self._in_td = True
            self._current_cell = ""
            self._td_count += 1
        elif tag == "span" and attrs_dict.get("class") == "driver_name":
            self._in_driver_name = True
            self._current_cell = ""

    def handle_endtag(self, tag):
        if tag == "tbody":
            self._in_tbody = False
        elif tag == "span" and self._in_driver_name:
            self._in_driver_name = False
            self._row_driver = self._current_cell.strip()
        elif tag == "td" and self._in_td:
            self._in_td = False
            if self._td_count == 1:
                self._row_pos = self._current_cell.strip()
        elif tag == "tr" and self._in_tbody:
            if self._row_pos and self._row_driver:
                try:
                    pos = int(self._row_pos)
                    self.results.append((pos, self._row_driver))
                except ValueError:
                    pass

    def handle_data(self, data):
        if self._in_driver_name:
            self._current_cell += data
        elif self._in_td and self._td_count == 1 and not self._in_driver_name:
            self._current_cell += data

# ============================================================
# LEGACY OVERALL RANKING PARSER
# ============================================================

class LiveRCParser(HTMLParser):
    """Parses a saved LiveRC overall ranking page and extracts results by class."""

    def __init__(self):
        super().__init__()
        self.results = {}  # class_name -> [(pos, driver), ...]
        self._current_class = None
        self._in_tbody = False
        self._in_td = False
        self._row_cells = []
        self._current_cell = ""
        self._td_count = 0

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "div" and attrs_dict.get("class") == "class_header":
            self._in_class_header = True
            self._current_cell = ""
        elif tag == "tbody":
            self._in_tbody = True
        elif tag == "tr" and self._in_tbody:
            self._row_cells = []
            self._td_count = 0
        elif tag == "td" and self._in_tbody:
            self._in_td = True
            self._current_cell = ""
            self._td_count += 1

    def handle_endtag(self, tag):
        if tag == "div" and hasattr(self, '_in_class_header') and self._in_class_header:
            self._in_class_header = False
            self._current_class = self._current_cell.strip()
            self.results[self._current_class] = []
        elif tag == "tbody":
            self._in_tbody = False
        elif tag == "td" and self._in_td:
            self._in_td = False
            self._row_cells.append(self._current_cell.strip())
        elif tag == "tr" and self._in_tbody and self._current_class:
            # Row cells: [pos, brand(img), country(img), driver, result, race]
            # We only care about cells with text content
            text_cells = [c for c in self._row_cells if c]
            if len(text_cells) >= 3:
                try:
                    pos = int(text_cells[0])
                    driver = text_cells[1]
                    self.results[self._current_class].append((pos, driver))
                except (ValueError, IndexError):
                    pass

    def handle_data(self, data):
        if hasattr(self, '_in_class_header') and self._in_class_header:
            self._current_cell += data
        elif self._in_td:
            self._current_cell += data

# ============================================================
# ORCHESTRATORS
# ============================================================

def parse_heat_sheet(source):
    """Parse a heat sheet page (URL or file), follow result links, return results + pole data.

    Returns:
        results:     {class_name: [(pos, driver), ...]}
        poles:       {class_name: driver_name}
        result_urls: {class_name: url}
        event_date:  date string or None
    """
    html = fetch_html(source)
    parser = HeatSheetParser()
    parser.feed(html)
    parser.close()

    # Derive base URL for resolving relative links
    if source.startswith("http"):
        base_url = source.rsplit("/", 1)[0] + "/"
    else:
        base_url = None

    results = {}
    poles = {}
    result_urls = {}

    for class_name, qual_order, result_link in parser.classes:
        if qual_order:
            poles[class_name] = qual_order[0]

        if result_link:
            if result_link.startswith("http"):
                full_url = result_link
            elif base_url:
                full_url = urljoin(base_url, result_link)
            else:
                full_url = os.path.join(os.path.dirname(source), result_link.lstrip("/"))

            result_urls[class_name] = full_url
            print(f"    Fetching results: {full_url}")
            result_html = fetch_html(full_url)
            rp = RaceResultParser()
            rp.feed(result_html)
            results[class_name] = rp.results
        else:
            results[class_name] = [(i + 1, d) for i, d in enumerate(qual_order)]

    return results, poles, result_urls, parser.event_date


def parse_overall_ranking(source):
    """Parse an overall ranking page (legacy). Returns results dict, empty poles, empty urls, no date."""
    html = fetch_html(source)
    parser = LiveRCParser()
    parser.feed(html)
    return parser.results, {}, {}, None


def is_heat_sheet(source):
    """Auto-detect whether source is a heat sheet or overall ranking."""
    if "view_heat_sheet" in source:
        return True
    # For local files, peek at content
    if not source.startswith("http"):
        try:
            with open(source, "r", encoding="utf-8") as f:
                head = f.read(10000)
            if 'class="class_header"' in head and "A-Main" in head:
                return True
            if '<span class="class_header">' in head:
                return True
        except OSError:
            pass
    return False


def parse_race_source(source):
    """Parse a race source (URL or file), auto-detecting format.

    Returns:
        results:     {class_name: [(pos, driver), ...]}
        poles:       {class_name: driver_name}
        url:         source URL if it was a URL, else None
        result_urls: {class_name: url}
        event_date:  date string or None
    """
    race_url = source if source.startswith("http") else None

    if is_heat_sheet(source):
        results, poles, result_urls, event_date = parse_heat_sheet(source)
    else:
        results, poles, result_urls, event_date = parse_overall_ranking(source)

    return results, poles, race_url, result_urls, event_date


def pos_to_points(position, max_points, points_gap):
    """Convert finishing position to points. 1st=max_points, 2nd=max_points-gap, 3rd=2nd-1, etc."""
    if position == 1:
        return max_points
    pts = max_points - points_gap - (position - 2)
    return max(pts, 0)


def build_scoreboards(races_data, race_names, races_poles, cfg):
    """Build consolidated scoreboards from multiple races."""
    max_points = cfg["max_points"]
    points_gap = cfg["points_gap"]
    pole_points = cfg["pole_position_points"]

    all_classes = set()
    for race in races_data:
        all_classes.update(race.keys())

    scoreboards = {}
    for class_name in sorted(all_classes):
        drivers = {}
        for race_idx, race in enumerate(races_data):
            if class_name not in race:
                continue
            pole_driver = races_poles[race_idx].get(class_name)
            for pos, driver in race[class_name]:
                if driver not in drivers:
                    drivers[driver] = {
                        "name": driver,
                        "race_positions": [None] * len(races_data),
                        "race_points": [0] * len(races_data),
                        "race_pole": [False] * len(races_data),
                        "total": 0,
                        "races_entered": 0,
                    }
                points = pos_to_points(pos, max_points, points_gap)
                is_pole = (pole_driver and driver == pole_driver)
                if is_pole and pole_points > 0:
                    points += pole_points
                drivers[driver]["race_positions"][race_idx] = pos
                drivers[driver]["race_points"][race_idx] = points
                drivers[driver]["race_pole"][race_idx] = is_pole
                drivers[driver]["races_entered"] += 1

        # Calculate totals (no drop - JS handles toggle)
        for driver in drivers.values():
            driver["total"] = sum(driver["race_points"])

        # Sort: highest total first, then most races entered as tiebreaker
        sorted_drivers = sorted(drivers.values(), key=lambda d: (-d["total"], -d["races_entered"]))
        scoreboards[class_name] = sorted_drivers

    return scoreboards


def format_driver_name(name):
    """Convert 'JOHN DOE' to 'John Doe'."""
    return name.title()


def _build_event_links(race_names, race_urls, race_dates):
    parts = []
    for i in range(len(race_names)):
        if not race_urls[i]:
            continue
        date_div = f'<div class="race-date">{race_dates[i]}</div>' if race_dates[i] else ''
        parts.append(f'    <span class="event-link"><a href="{race_urls[i]}" target="_blank">{race_names[i]}</a>{date_div}</span>')
    return "\n".join(parts)


def generate_html(scoreboards, race_names, race_urls, race_dates, races_result_urls, cfg):
    """Generate the scoreboard HTML page."""
    title = cfg["title"]
    track = cfg.get("track")
    logo = cfg.get("logo")
    max_points = cfg["max_points"]
    points_gap = cfg["points_gap"]
    pole_points = cfg["pole_position_points"]
    series_length = cfg["series_length"]

    # Prepare data for JSON embedding
    json_data = {}
    for class_name, drivers in scoreboards.items():
        json_data[class_name] = []
        for d in drivers:
            json_data[class_name].append({
                "name": format_driver_name(d["name"]),
                "race_points": d["race_points"],
                "race_positions": d["race_positions"],
                "race_pole": d["race_pole"],
                "races_entered": d["races_entered"],
            })

    # Build race links data — pad to series_length for unfinished races
    race_links = []
    for i in range(series_length):
        if i < len(race_names):
            race_links.append({"name": race_names[i], "url": race_urls[i]})
        else:
            race_links.append({"name": f"Race {i + 1}", "url": None})

    # Pad per-driver data to series_length
    for class_name in json_data:
        for d in json_data[class_name]:
            while len(d["race_points"]) < series_length:
                d["race_points"].append(0)
                d["race_positions"].append(None)
                d["race_pole"].append(False)

    # Build per-class result links: {class_name: [{name, url}, ...]}
    class_result_links = {}
    for class_name in scoreboards:
        links = []
        for i, name in enumerate(race_names):
            url = races_result_urls[i].get(class_name)
            if url:
                links.append({"name": name, "url": url})
        class_result_links[class_name] = links

    track_html = f'<div class="track">{track}</div>' if track else ''
    if logo:
        header_html = (
            f'<div class="page-header">'
            f'<img class="logo" src="{logo}" alt="Logo">'
            f'<div class="header-text">{track_html}<h1>{title}</h1></div>'
            f'</div>'
        )
    else:
        header_html = f'{track_html}<h1>{title}</h1>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}

        body {{
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: #1a1a2e;
            color: #e0e0e0;
            padding: 20px;
        }}

        h1 {{
            text-align: center;
            color: #e94560;
            margin-bottom: 10px;
            font-size: 2em;
        }}

        .track {{
            color: #aaa;
            font-size: 1em;
            font-weight: 600;
            letter-spacing: 1px;
            text-transform: uppercase;
            margin-bottom: 4px;
            text-align: center;
        }}

        .page-header {{
            position: relative;
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 160px;
            margin-bottom: 10px;
        }}
        .page-header .logo {{
            position: absolute;
            left: 0;
            top: 50%;
            transform: translateY(-50%);
            width: 160px;
            height: 160px;
            object-fit: contain;
        }}
        .page-header .header-text {{
            text-align: center;
        }}
        .page-header h1 {{
            margin-bottom: 0;
            text-align: center;
        }}
        .page-header .track {{
            text-align: center;
        }}

        .subtitle {{
            text-align: center;
            color: #fff;
            margin-bottom: 15px;
            font-size: 0.9em;
        }}

        .class-section {{
            background: #16213e;
            border-radius: 10px;
            margin-bottom: 30px;
            overflow: hidden;
            box-shadow: 0 4px 15px rgba(0,0,0,0.3);
        }}

        .class-header {{
            background: #0f3460;
            padding: 15px 20px;
            font-size: 1.3em;
            font-weight: bold;
            color: #e94560;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}

        .class-header:hover {{ background: #1a4a7a; }}

        .class-header .toggle {{ font-size: 0.8em; color: #888; }}

        .event-links {{
            text-align: center;
            margin-bottom: 25px;
            font-size: 0.9em;
        }}
        .event-link {{
            display: inline-block;
            text-align: center;
            margin: 0 15px;
        }}
        .event-links a {{
            color: #4caf50;
            text-decoration: none;
            border-bottom: 1px dashed #666;
        }}
        .event-links a:hover {{
            color: #81c784;
            border-bottom-color: #e94560;
        }}
        .event-links .race-date {{
            color: #4caf50;
            font-size: 0.8em;
            margin-top: 2px;
        }}

        .class-body {{ padding: 0; overflow-x: auto; }}

        table {{ width: auto; border-collapse: collapse; table-layout: fixed; }}

        thead th {{
            background: #1a1a3e;
            padding: 10px 12px;
            text-align: left;
            font-size: 0.85em;
            color: #aaa;
            border-bottom: 2px solid #333;
            position: sticky;
            top: 0;
            overflow: hidden;
            text-overflow: ellipsis;
        }}

        thead th.race-col {{ text-align: center; }}
        thead th.race-col.completed {{ color: #4caf50; }}
        thead th.race-col.completed a {{ color: #4caf50; }}
        thead th.race-col.completed a:hover {{ color: #81c784; border-bottom-color: #81c784; }}
        thead th.race-col.pending {{ color: #ffd700; }}
        thead th.total-col {{ text-align: center; color: #fff; }}

        thead th a {{
            color: #aaa;
            text-decoration: none;
            border-bottom: 1px dashed #666;
        }}
        thead th a:hover {{
            color: #e94560;
            border-bottom-color: #e94560;
        }}

        tbody td {{
            padding: 10px 12px;
            border-bottom: 1px solid #2a2a4e;
        }}

        tbody tr:hover {{ background: #1f2f50; }}
        tbody tr.rank-1 td {{ background: rgba(255, 215, 0, 0.08); }}
        tbody tr.rank-2 td {{ background: rgba(192, 192, 192, 0.06); }}
        tbody tr.rank-3 td {{ background: rgba(205, 127, 50, 0.06); }}

        .pos-col {{ font-weight: bold; text-align: center; }}
        .pos-1 {{ color: #ffd700; }}
        .pos-2 {{ color: #c0c0c0; }}
        .pos-3 {{ color: #cd7f32; }}

        .driver-col {{ font-weight: 600; white-space: nowrap; }}

        .race-result {{ text-align: center; font-size: 0.9em; font-weight: bold; }}
        .race-result .dns {{ color: #666; font-style: italic; font-weight: normal; }}
        .race-result .pole-marker {{ color: #ffd700; font-size: 0.7em; vertical-align: super; margin-left: 1px; }}
        .race-result .finish-pos {{ color: #fff; font-size: 0.75em; margin-left: 2px; font-weight: normal; }}
        .race-result.dropped {{ color: #888; font-weight: normal; }}
        .race-result.dropped .finish-pos {{ color: #888; }}
        .race-result.dropped .pole-marker {{ color: #888; }}

        .total-col-val {{
            text-align: center;
            font-weight: normal;
            font-size: 0.95em;
            color: #888;
        }}

        .drop-col-val {{
            text-align: center;
            font-weight: bold;
            font-size: 1.1em;
            color: #fff;
        }}

        @media (max-width: 768px) {{
            body {{ padding: 10px; }}
            h1 {{ font-size: 1.5em; }}
            thead th, tbody td {{ padding: 6px 8px; font-size: 0.8em; }}
        }}
    </style>
</head>
<body>

{header_html}
<div class="subtitle">Points: 1st = {max_points}pts, 2nd = {max_points - points_gap}pts, 3rd = {max_points - points_gap - 1}pts, ...{f" Pole = +{pole_points}pt." if pole_points else ""} Missed race = 0pts.</div>
<div class="subtitle">Race {len(race_names)} of {series_length} completed</div>

<div class="event-links">
{_build_event_links(race_names, race_urls, race_dates)}
</div>

<div id="scoreboards"></div>

<script>
const RACE_LINKS = {json.dumps(race_links)};
const SCOREBOARDS = {json.dumps(json_data)};
const CLASS_RESULT_LINKS = {json.dumps(class_result_links)};
const MAX_POINTS = {max_points};
const POLE_BONUS = {pole_points};

function ordinal(n) {{
    const s = ['th','st','nd','rd'];
    const v = n % 100;
    return n + (s[(v-20)%10] || s[v] || s[0]);
}}

function renderAll() {{
    const container = document.getElementById('scoreboards');
    container.innerHTML = '';

    // Find longest driver name across all classes
    let globalMaxNameLen = 0;
    for (const drivers of Object.values(SCOREBOARDS)) {{
        for (const d of drivers) {{
            if (d.name.length > globalMaxNameLen) globalMaxNameLen = d.name.length;
        }}
    }}
    const driverColWidth = Math.max(120, globalMaxNameLen * 9 + 60);

    for (const [className, drivers] of Object.entries(SCOREBOARDS)) {{
        const scored = drivers.map(d => {{
            const pts = [...d.race_points];
            const total = pts.reduce((a, b) => a + b, 0);

            // Find worst race for drop calculation
            let worstIdx = 0;
            for (let i = 1; i < pts.length; i++) {{
                if (pts[i] < pts[worstIdx]) worstIdx = i;
            }}
            const dropTotal = pts.reduce((a, b, i) => i === worstIdx ? a : a + b, 0);

            return {{ ...d, total, dropTotal, droppedIdx: worstIdx }};
        }});

        scored.sort((a, b) => b.dropTotal - a.dropTotal || b.total - a.total || b.races_entered - a.races_entered);

        const section = document.createElement('div');
        section.className = 'class-section';

        const header = document.createElement('div');
        header.className = 'class-header';
        header.innerHTML = `<span>${{className}}</span>`;
        section.appendChild(header);

        const body = document.createElement('div');
        body.className = 'class-body';

        const nRaces = RACE_LINKS.length;
        const classLinks = CLASS_RESULT_LINKS[className] || [];
        let t = '<table><colgroup>';
        t += '<col style="width:40px">';
        t += `<col style="width:${{driverColWidth}}px">`;
        t += '<col style="width:80px">';
        t += '<col style="width:120px">';
        for (let i = 0; i < nRaces; i++) t += '<col style="width:95px">';
        t += '</colgroup>';
        t += '<thead><tr><th class="pos-col">#</th><th>Driver</th>';
        t += '<th class="total-col">Total</th><th class="total-col">Total w/o drops</th>';
        RACE_LINKS.forEach((rl, idx) => {{
            const classLink = classLinks.find(cl => cl.name === rl.name);
            const status = rl.url ? 'completed' : 'pending';
            if (classLink && classLink.url) {{
                t += `<th class="race-col ${{status}}"><a href="${{classLink.url}}" target="_blank" title="View results">${{rl.name}}</a></th>`;
            }} else {{
                t += `<th class="race-col ${{status}}">${{rl.name}}</th>`;
            }}
        }});
        t += '</tr></thead><tbody>';

        scored.forEach((driver, idx) => {{
            const pos = idx + 1;
            const posClass = pos <= 3 ? `pos-${{pos}}` : '';
            const rankClass = pos <= 3 ? `rank-${{pos}}` : '';

            t += `<tr class="${{rankClass}}">`;
            t += `<td class="pos-col ${{posClass}}">${{pos}}</td>`;
            t += `<td class="driver-col">${{driver.name}}</td>`;
            t += `<td class="drop-col-val">${{driver.dropTotal}}</td>`;
            t += `<td class="total-col-val">${{driver.total}}</td>`;

            driver.race_points.forEach((pts, raceIdx) => {{
                const isPole = driver.race_pole && driver.race_pole[raceIdx];
                const poleTag = isPole ? '<span class="pole-marker">P</span>' : '';
                const racePos = driver.race_positions[raceIdx];
                const dropClass = raceIdx === driver.droppedIdx ? ' dropped' : '';
                if (racePos === null) {{
                    t += `<td class="race-result${{dropClass}}"><span class="dns">-</span></td>`;
                }} else {{
                    const posTag = `<span class="finish-pos">(${{ordinal(racePos)}})</span>`;
                    t += `<td class="race-result${{dropClass}}"><span class="position">${{pts}}</span>${{poleTag}} ${{posTag}}</td>`;
                }}
            }});

            t += '</tr>';
        }});

        t += '</tbody></table>';
        body.innerHTML = t;
        section.appendChild(body);
        container.appendChild(section);
    }}
}}

document.addEventListener('DOMContentLoaded', renderAll);
</script>

</body>
</html>
"""
    return html


def process_series(config_path):
    """Process a single series from a JSON config file."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_dir = os.path.dirname(os.path.abspath(config_path))
    cfg = load_config(config_path)

    print(f"=== {cfg['title']} ===")
    race_names = []
    race_urls = []
    race_dates = []
    races_data = []
    races_poles = []
    races_result_urls = []

    for idx, source in enumerate(cfg["races"], 1):
        if not source.startswith("http"):
            source = os.path.join(config_dir, source)

        name = f"Race {idx}"
        print(f"Parsing {name}: {source}")
        results, poles, url, result_urls, event_date = parse_race_source(source)
        for cls, drivers in results.items():
            pole_msg = f" (pole: {poles[cls]})" if cls in poles else ""
            print(f"  {cls}: {len(drivers)} drivers{pole_msg}")
        if event_date:
            print(f"  Date: {event_date}")
        race_names.append(name)
        race_urls.append(url)
        race_dates.append(event_date)
        races_data.append(results)
        races_poles.append(poles)
        races_result_urls.append(result_urls)

    print(f"\nBuilding scoreboards...")
    scoreboards = build_scoreboards(races_data, race_names, races_poles, cfg)

    output_name = os.path.splitext(os.path.basename(config_path))[0] + ".html"
    output_path = os.path.join(config_dir, output_name)
    html = generate_html(scoreboards, race_names, race_urls, race_dates, races_result_urls, cfg)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)

    print(f"Scoreboard written to {output_path}")
    print(f"Classes: {len(scoreboards)}")
    total_drivers = sum(len(d) for d in scoreboards.values())
    print(f"Total driver entries: {total_drivers}\n")


def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 build_scoreboard.py <config.json>")
        print("  python3 build_scoreboard.py --all")
        print()
        print("Use --all to process all .json config files in the current directory.")
        sys.exit(1)

    if sys.argv[1] == "--all":
        script_dir = os.path.dirname(os.path.abspath(__file__))
        configs = sorted(f for f in os.listdir(script_dir) if f.endswith(".json"))
        if not configs:
            print("No .json config files found.")
            sys.exit(1)
        for cfg_file in configs:
            process_series(os.path.join(script_dir, cfg_file))
    else:
        for config_path in sys.argv[1:]:
            process_series(config_path)


if __name__ == "__main__":
    main()
