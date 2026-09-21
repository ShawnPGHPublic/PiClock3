"""NOAA tide curve and high/low times for a PiClock3 region."""

import datetime
import json
import logging
import math

from PyQt5.QtCore import QPointF, Qt, QTimer
from PyQt5.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen
from PyQt5.QtNetwork import QNetworkReply
from PyQt5.QtWidgets import QWidget

from PiClock3.WebGet import WebGet
from PiClock3.Widget import Widget

logger = logging.getLogger(__name__)

NOAA = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
MDAPI = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json"


def _qcolor(value, fallback):
    c = QColor(value) if value else QColor()
    return c if c.isValid() else QColor(fallback)


class TideChart(QWidget):
    """Paints the 24-hour curve; the footer is drawn in the lower band."""

    def __init__(self, parent, plugin):
        super().__init__(parent)
        self.plugin = plugin
        self.setAttribute(Qt.WA_TranslucentBackground, True)

    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            self.plugin.paintChart(self, painter)
        finally:
            if painter.isActive():
                painter.end()


class Tides(Widget):

    def __init__(self, piclock, name, config):
        super().__init__(piclock, name, config)
        self.chart = None
        self.timer = None
        self.fetch_timer = None
        self.series = []          # [(datetime, feet), ...]
        self.extrema = []         # [{t, v, type: 'H'|'L'}, ...]
        self.station = None
        self.station_name = ""
        self.status = "Loading tides…"
        self._fetching = False

    def start(self):
        rect = self.region.frameRect()
        self.chart = TideChart(self.region, self)
        self.chart.setGeometry(rect)
        self.chart.setObjectName("tides")
        self.applyEffect(self.chart, rect.height())

        self.timer = QTimer()
        self.timer.timeout.connect(self.redraw)
        self.timer.start(30 * 1000)

        self.fetch_timer = QTimer()
        self.fetch_timer.timeout.connect(self.refresh)
        self.fetch_timer.start(int(self.config["refresh"]) * 60 * 1000)

        self.refresh()

    def pageChange(self):
        self.redraw()

    def redraw(self):
        if self.chart is None:
            return
        rect = self.region.frameRect()
        if self.chart.geometry() != rect:
            self.chart.setGeometry(rect)
        if self.region.isVisible():
            self.chart.update()

    def refresh(self):
        if not self.region.isVisible() and self.series:
            return
        if self._fetching:
            return
        station = str(self.config.get("station") or "").strip()
        if station:
            self.station = station
            self.fetchPredictions()
            return
        self.findStation()

    def findStation(self):
        lat = self.expand("{location.latitude}")
        lon = self.expand("{location.longitude}")
        url = (
            f"{MDAPI}?type=tidepredictions&lat={lat}&lon={lon}"
            f"&radius=80&units=english"
        )
        logger.info("tides nearest-station %s", url)
        self._fetching = True
        WebGet(url, self.gotStations)

    def gotStations(self, error, data, params):
        self._fetching = False
        if error != QNetworkReply.NoError or not data:
            self.status = "No NOAA station nearby"
            logger.warning("tides station lookup failed: %s", error)
            self.redraw()
            return
        try:
            body = json.loads(bytes(data).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            self.status = "Bad station list"
            logger.warning("tides station json: %s", e)
            self.redraw()
            return
        stations = body.get("stations") or []
        if not stations:
            self.status = "No tide station for this location"
            self.redraw()
            return

        try:
            lat = float(self.expand("{location.latitude}"))
            lon = float(self.expand("{location.longitude}"))
        except (TypeError, ValueError):
            pick = stations[0]
        else:
            def dist(s):
                try:
                    return (float(s.get("lat", 0)) - lat) ** 2 + (
                        float(s.get("lng", s.get("lon", 0))) - lon
                    ) ** 2
                except (TypeError, ValueError):
                    return 1e9
            pick = min(stations, key=dist)

        self.station = str(pick.get("id") or pick.get("id", ""))
        self.station_name = pick.get("name") or self.station
        logger.info("tides using station %s %s", self.station, self.station_name)
        self.fetchPredictions()

    def fetchPredictions(self):
        if not self.station:
            return
        now = self.now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        begin = start.strftime("%Y%m%d")
        units = "english" if self.config.get("height-units", "feet") == "feet" else "metric"
        self._common = (
            f"station={self.station}"
            f"&product=predictions&datum={self.config['datum']}"
            f"&time_zone=lst_ldt&units={units}&format=json"
            f"&application=PiClock3-tides"
            f"&begin_date={begin}&range=36"
        )
        hilo = f"{NOAA}?{self._common}&interval=hilo"
        logger.info("tides hilo %s", hilo)
        self._fetching = True
        WebGet(hilo, self.gotHiLo)

    def gotCurve(self, error, data, params):
        self._fetching = False
        self.series = []
        ok = error == QNetworkReply.NoError and data
        body = {}
        if ok:
            try:
                body = json.loads(bytes(data).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                body = {}
        if body.get("error") or not (body.get("predictions") or []):
            logger.info("tides no 6-min series; smoothing high/low instead")
            self.series = self.curveFromExtrema()
        else:
            for row in body.get("predictions") or []:
                t = self.parseNOAA(row.get("t"))
                try:
                    v = float(row.get("v"))
                except (TypeError, ValueError):
                    continue
                if t is not None:
                    self.series.append((t, v))
        if self.series:
            self.status = ""
        elif self.extrema:
            self.series = self.curveFromExtrema()
            self.status = ""
        else:
            self.status = "No tide data"
        self.redraw()
        
    def curveFromExtrema(self):
        """Smooth cosine segments between published high/low points."""
        pts = sorted(self.extrema, key=lambda e: e["t"])
        if len(pts) < 2:
            return [(e["t"], e["v"]) for e in pts]
        out = []
        step = datetime.timedelta(minutes=6)
        for i in range(len(pts) - 1):
            a, b = pts[i], pts[i + 1]
            span = (b["t"] - a["t"]).total_seconds()
            if span <= 0:
                continue
            t = a["t"]
            while t < b["t"]:
                f = (t - a["t"]).total_seconds() / span
                # half-cosine: flat at each extremum, smooth in between
                w = 0.5 - 0.5 * math.cos(math.pi * f)
                v = a["v"] + (b["v"] - a["v"]) * w
                out.append((t, v))
                t += step
        out.append((pts[-1]["t"], pts[-1]["v"]))
        return out
                
    def gotHiLo(self, error, data, params):
        self.extrema = []
        if error != QNetworkReply.NoError or not data:
            self._fetching = False
            self.status = "Tide fetch failed"
            logger.warning("tides hilo failed: %s", error)
            self.redraw()
            return
        try:
            body = json.loads(bytes(data).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            self._fetching = False
            self.status = "Bad tide data"
            logger.warning("tides hilo json: %s", e)
            self.redraw()
            return
        if body.get("error"):
            self._fetching = False
            self.status = body["error"].get("message", "NOAA error")
            logger.warning("tides NOAA: %s", self.status)
            self.redraw()
            return
        for row in body.get("predictions") or []:
            t = self.parseNOAA(row.get("t"))
            try:
                v = float(row.get("v"))
            except (TypeError, ValueError):
                continue
            kind = (row.get("type") or "").upper()[:1] or "?"
            if t is not None:
                self.extrema.append({"t": t, "v": v, "type": kind})
        curve = f"{NOAA}?{self._common}&interval=6"
        logger.info("tides curve %s", curve)
        WebGet(curve, self.gotCurve)

    def parseNOAA(self, text):
        if not text:
            return None
        try:
            naive = datetime.datetime.strptime(text, "%Y-%m-%d %H:%M")
        except ValueError:
            return None
        now = self.now()
        if now.tzinfo is not None:
            return naive.replace(tzinfo=now.tzinfo)
        return naive

    def paintChart(self, widget, painter):
        painter.setRenderHint(QPainter.Antialiasing, True)
        w, h = widget.width(), widget.height()
        if w < 8 or h < 8:
            return

        footer_h = max(18, int(h * float(self.config["footer-height"])))
        chart_h = max(1, h - footer_h)
        pad = max(6, int(min(w, chart_h) * 0.04))

        color = _qcolor(self.color(), "#7ec8e3")
        fill = _qcolor(self.config.get("fill-color"), "#1b6b8a")
        fill.setAlpha(int(float(self.config.get("fill-opacity", 0.45)) * 255))
        now_color = _qcolor(self.config.get("now-color"), "#f4d35e")
        hi_color = _qcolor(self.config.get("high-color"), "#e8f4ff")
        lo_color = _qcolor(self.config.get("low-color"), "#9bb8c9")
        grid = QColor(color)
        grid.setAlpha(50)

        if self.status or len(self.series) < 2:
            painter.setPen(color)
            font = widget.font()
            font.setPixelSize(max(10, int(h * 0.12)))
            painter.setFont(font)
            painter.drawText(widget.rect(), Qt.AlignCenter, self.status or "No tide data")
            return

        now = self.now()
        day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day1 = day0 + datetime.timedelta(hours=24)
        window = [(t, v) for t, v in self.series if day0 <= t <= day1]
        if len(window) < 2:
            window = self.series

        t0, t1 = window[0][0], window[-1][0]
        span = max((t1 - t0).total_seconds(), 1.0)
        vals = [v for _, v in window]
        vmin, vmax = min(vals), max(vals)
        if abs(vmax - vmin) < 0.15:
            vmax += 0.5
            vmin -= 0.5
        else:
            pad_v = (vmax - vmin) * 0.12
            vmax += pad_v
            vmin -= pad_v

        def x_of(t):
            return pad + (t - t0).total_seconds() / span * (w - 2 * pad)

        def y_of(v):
            return pad + (1.0 - (v - vmin) / (vmax - vmin)) * (chart_h - 2 * pad)

        # hour ticks
        painter.setPen(QPen(grid, 1))
        hour = t0.replace(minute=0, second=0, microsecond=0)
        if hour < t0:
            hour += datetime.timedelta(hours=1)
        while hour <= t1:
            x = x_of(hour)
            painter.drawLine(QPointF(x, pad), QPointF(x, chart_h - pad))
            hour += datetime.timedelta(hours=3)

        points = [QPointF(x_of(t), y_of(v)) for t, v in window]
        path = QPainterPath(points[0])
        for i in range(1, len(points)):
            prev = points[i - 1]
            cur = points[i]
            dx = (cur.x() - prev.x()) * 0.4
            path.cubicTo(
                QPointF(prev.x() + dx, prev.y()),
                QPointF(cur.x() - dx, cur.y()),
                cur,
            )

        fill_path = QPainterPath(path)
        fill_path.lineTo(QPointF(points[-1].x(), chart_h - pad))
        fill_path.lineTo(QPointF(points[0].x(), chart_h - pad))
        fill_path.closeSubpath()
        grad = QLinearGradient(0, pad, 0, chart_h)
        top = QColor(fill)
        top.setAlpha(min(255, fill.alpha() + 40))
        bot = QColor(fill)
        bot.setAlpha(max(20, fill.alpha() // 3))
        grad.setColorAt(0.0, top)
        grad.setColorAt(1.0, bot)
        painter.fillPath(fill_path, grad)

        pen = QPen(color, max(1.6, w / 280.0))
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)

        if t0 <= now <= t1:
            nx = x_of(now)
            npen = QPen(now_color, 1.5, Qt.DashLine)
            painter.setPen(npen)
            painter.drawLine(QPointF(nx, pad), QPointF(nx, chart_h - pad))
            # height at "now"
            vnow = self.interp(now)
            if vnow is not None:
                painter.setBrush(now_color)
                painter.setPen(Qt.NoPen)
                painter.drawEllipse(QPointF(nx, y_of(vnow)), 4, 4)

        painter.setPen(Qt.NoPen)
        for ext in self.extrema:
            if not (t0 <= ext["t"] <= t1):
                continue
            c = hi_color if ext["type"] == "H" else lo_color
            painter.setBrush(c)
            painter.drawEllipse(QPointF(x_of(ext["t"]), y_of(ext["v"])), 3.2, 3.2)

        self.paintFooter(painter, w, h, footer_h, color, hi_color, lo_color, now)

    def interp(self, when):
        for i in range(1, len(self.series)):
            t0, v0 = self.series[i - 1]
            t1, v1 = self.series[i]
            if t0 <= when <= t1:
                span = (t1 - t0).total_seconds() or 1.0
                f = (when - t0).total_seconds() / span
                return v0 + (v1 - v0) * f
        return None

    def paintFooter(self, painter, w, h, footer_h, color, hi_color, lo_color, now):
        y = h - footer_h
        painter.setPen(QColor(color.red(), color.green(), color.blue(), 60))
        painter.drawLine(8, y, w - 8, y)

        unit = "ft" if self.config.get("height-units", "feet") == "feet" else "m"
        fmt = self.config.get("time-format") or "%-I:%M%p"
        upcoming = [e for e in self.extrema if e["t"] >= now - datetime.timedelta(minutes=20)]
        if not upcoming:
            upcoming = self.extrema[:4]
        upcoming = upcoming[:4]

        parts = []
        for e in upcoming:
            try:
                clock = e["t"].strftime(fmt).replace("AM", "a").replace("PM", "p")
            except ValueError:
                clock = e["t"].strftime("%H:%M")
            label = "H" if e["type"] == "H" else "L"
            parts.append(f"{label} {clock} {e['v']:.1f}{unit}")

        name = self.station_name or self.station or ""
        line = "  ·  ".join(parts) if parts else "No high/low times"
        if name and self.config.get("show-station", True):
            line = f"{name}  {line}"

        side = 8
        avail = max(16, w - 2 * side)

        font = QFont(painter.font())
        px = max(8, int(footer_h * float(self.config["footer-font-size"])))
        while px > 7:
            font.setPixelSize(px)
            painter.setFont(font)
            if painter.fontMetrics().horizontalAdvance(line) <= avail:
                break
            px -= 1

        painter.setPen(color)
        painter.drawText(
            side, y, avail, footer_h,
            Qt.AlignCenter | Qt.TextSingleLine,
            line,
        )
