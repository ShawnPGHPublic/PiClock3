# PiClock3 tides

Widget plugin: a smooth 24-hour NOAA tide curve with high/low times in the footer.

Data: `https://api.tidesandcurrents.noaa.gov/api/prod/datagetter`  
No API key.

<img width="433" height="321" alt="image" src="https://github.com/user-attachments/assets/c250f7fb-7ebe-4b42-a632-fe15e8de335d" />


## Install

From the PiClock3 checkout:

```bash
mkdir -p plugins/tides
# copy the plugin files into plugins/tides/
python3 PyQtPiClock3.py plugins/tides/examples/tides.yaml

Set in your config:
widgets:
  tides:
    plugin: plugins.tides
    region: bottom
    station: "8720218"    # optional NOAA id
    
If station is empty, the plugin picks the nearest tidepredictions station
to location.latitude / location.longitude.

What you seeMost of the box: today’s predicted water level (6-minute NOAA series,
cubic-smoothed), filled under the curve.
Dashed gold line: now.
Dots: published high and low tides.
Footer: station name and the next highs/lows with clock time and height.

units: feet or meters.  Heights use datum MLLW unless you change datum.

---

### Use it

1. Put the files in `plugins/tides/` next to `Config.yaml`.
2. Point a widget at `plugin: plugins.tides` and a `region` (the example uses `bottom`).
3. Set `location` to the coast you care about, or set `station` to a [NOAA station id](https://tidesandcurrents.noaa.gov/stations.html?type=Tide+Predictions).

```bash
python3 PyQtPiClock3.py plugins/tides/examples/tides.yaml --check
python3 PyQtPiClock3.py plugins/tides/examples/tides.yaml

The example location is Daytona Beach; NOAA should resolve a nearby prediction station automatically. For a known gauge, set station (for example Daytona-area ids around 8720218 / 8720211 — confirm on the NOAA station map for the exact gauge you want).




