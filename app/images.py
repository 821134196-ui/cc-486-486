"""本地目录存放车辆照片与磅单图片：用 SVG 生成可复现的虚构图片。"""
from __future__ import annotations

from pathlib import Path

VEHICLE_DIR = Path("data/images/vehicles")
SLIP_DIR = Path("data/images/slips")

COLORS = {
    "cement": "#9aa3ab",
    "yellow": "#f5b301",
    "navy": "#1f3a5f",
    "ink": "#2b2f33",
}


def vehicle_svg(plate: str, model: str) -> str:
    tail4 = plate[-4:]
    return f"""<svg xmlns='http://www.w3.org/2000/svg' width='320' height='180' viewBox='0 0 320 180'>
  <rect width='320' height='180' fill='{COLORS['cement']}'/>
  <rect y='120' width='320' height='60' fill='#6f7680'/>
  <rect x='30' y='55' width='150' height='70' rx='8' fill='{COLORS['navy']}'/>
  <rect x='180' y='70' width='70' height='55' rx='6' fill='{COLORS['yellow']}'/>
  <circle cx='75' cy='132' r='20' fill='#222'/><circle cx='75' cy='132' r='9' fill='#aaa'/>
  <circle cx='220' cy='132' r='20' fill='#222'/><circle cx='220' cy='132' r='9' fill='#aaa'/>
  <text x='160' y='30' font-family='monospace' font-size='22' font-weight='bold'
        text-anchor='middle' fill='{COLORS['ink']}'>散装砂浆车 · {tail4}</text>
  <text x='160' y='105' font-family='sans-serif' font-size='15'
        text-anchor='middle' fill='white'>{model}</text>
  <text x='160' y='170' font-family='sans-serif' font-size='10'
        text-anchor='middle' fill='#eee'>虚构车辆图片 FAKE VEHICLE</text>
</svg>"""


def slip_svg(ticket: str, plate: str, kind: str, weight_kg: float, extra: str = "") -> str:
    return f"""<svg xmlns='http://www.w3.org/2000/svg' width='300' height='220' viewBox='0 0 300 220'>
  <rect width='300' height='220' fill='white' stroke='{COLORS['navy']}' stroke-width='3'/>
  <rect width='300' height='34' fill='{COLORS['yellow']}'/>
  <text x='150' y='24' font-family='sans-serif' font-size='18' font-weight='bold'
        text-anchor='middle' fill='{COLORS['navy']}'>磅 单 {kind}</text>
  <text x='20' y='70' font-family='monospace' font-size='14' fill='{COLORS['ink']}'>票号 {ticket}</text>
  <text x='20' y='100' font-family='monospace' font-size='14' fill='{COLORS['ink']}'>车牌 {plate}</text>
  <text x='20' y='146' font-family='monospace' font-size='30' font-weight='bold'
        fill='{COLORS['navy']}'>{weight_kg:.1f} kg</text>
  <text x='20' y='180' font-family='monospace' font-size='11' fill='#555'>{extra}</text>
  <text x='20' y='205' font-family='monospace' font-size='10' fill='#999'>虚构磅单 FAKE WEIGH TICKET</text>
</svg>"""


def save_vehicle(plate: str, model: str) -> str:
    VEHICLE_DIR.mkdir(parents=True, exist_ok=True)
    p = VEHICLE_DIR / f"{plate}.svg"
    p.write_text(vehicle_svg(plate, model), encoding="utf-8")
    return str(p)


def save_slip(ticket: str, plate: str, kind: str, weight_kg: float, extra: str = "") -> str:
    SLIP_DIR.mkdir(parents=True, exist_ok=True)
    p = SLIP_DIR / f"{ticket}.svg"
    p.write_text(slip_svg(ticket, plate, kind, weight_kg, extra), encoding="utf-8")
    return str(p)
