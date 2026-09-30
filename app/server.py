"""HTTP 服务：排队大屏、登记、两次过磅、装料补装、出场核验、结算复核。

仅用 Python 标准库。当前操作员通过 cookie `op` 切换（演示角色分工）。
"""
from __future__ import annotations

import html
import os
import sqlite3
import urllib.parse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import adapters, scheduler, seed, store
from .db import get_db, init_db

STATUS_CN = {
    "PAID": "已付款待入场", "QUEUED": "入场排队", "TARE": "已皮重·待装料",
    "LOADING": "装料中", "LOADED": "装料完成·待复磅", "WEIGHED": "已复磅·待确认",
    "CONFIRMED": "已确认·待放行", "RELEASED": "已放行出场",
    "REJECTED": "客户拒签", "NO_SHOW": "过号释放", "CANCELLED": "已取消退款",
}
KIND_CN = {
    "VEHICLE_MISMATCH": "车型不符", "SILO_WRONG": "料仓错选",
    "TARE_ABNORMAL": "皮重异常", "OVERLOAD": "装料超量",
    "SCALE_DOWN": "地磅中断", "MOISTURE_DISPUTE": "含水争议",
    "REFILL_EXCEED": "补装后超单", "CUSTOMER_REJECT": "客户拒签",
    "NO_SHOW": "过号释放", "LOCK_EXCEEDED": "锁定量不足",
}
EVENT_CN = {"TARE": "皮重", "GROSS": "毛重", "LOAD": "装料", "REFILL": "补装",
            "MOISTURE": "含水裁定", "MANUAL": "人工改重"}
ROLE_CN = {"dispatcher": "调度", "weigher": "司磅", "loader": "装载工",
           "cashier": "收银", "supervisor": "主管"}

USERS_FALLBACK = [
    ("zhang", "张调度", "dispatcher"), ("li", "李司磅", "weigher"),
    ("wang", "王装载", "loader"), ("zhao", "赵收银", "cashier"),
    ("sun", "孙主管", "supervisor"),
]


def esc(x) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def users():
    rows = get_db().execute("SELECT * FROM users ORDER BY role").fetchall()
    return rows or [dict(zip(("username", "display", "role"), u)) for u in USERS_FALLBACK]


class Handler(BaseHTTPRequestHandler):
    server_version = "MortarYard/1.0"

    # ------------------------------------------------ 基础
    def log_message(self, fmt, *args):
        pass  # 静默；需要时可打开

    def _send(self, body: str | bytes, status: int = 200, ctype: str = "text/html; charset=utf-8",
              extra_headers=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, path: str):
        self._send("", 303, extra_headers=[("Location", path)])

    def _form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}

    def _query(self) -> dict[str, str]:
        q = urllib.parse.urlparse(self.path).query
        return {k: v[0] for k, v in urllib.parse.parse_qs(q).items()}

    def _operator(self) -> sqlite3.Row | None:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        name = cookie["op"].value if "op" in cookie else "zhang"
        row = get_db().execute("SELECT * FROM users WHERE username=?", (name,)).fetchone()
        if row:
            return row
        return sqlite3.Row(None, ("username", "display", "role"), (name, name, "dispatcher"))  # type: ignore

    def _flash_path(self, base: str, msg: str | None, err: str | None) -> str:
        qs = {}
        if msg:
            qs["msg"] = msg
        if err:
            qs["err"] = err
        return base + ("?" + urllib.parse.urlencode(qs) if qs else "")

    # ------------------------------------------------ 路由
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/" or path == "/board":
                return self.page_board()
            if path == "/orders":
                return self.page_orders()
            if path == "/orders/new":
                return self.page_new_order()
            if path.startswith("/orders/"):
                code = path.split("/")[2]
                return self.page_order(code)
            if path == "/weigh":
                return self.page_weigh()
            if path == "/exceptions":
                return self.page_exceptions()
            if path == "/reviews":
                return self.page_reviews()
            if path == "/silos":
                return self.page_silos()
            if path.startswith("/img/"):
                return self.static_image(path[len("/img/"):])
            if path == "/static/style.css":
                return self.serve_css()
            self._send("404", 404, "text/plain; charset=utf-8")
        except Exception as exc:  # 页面错误不崩进程
            self._send(f"<h1>服务器错误</h1><pre>{esc(exc)}</pre>", 500)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        form = self._form()
        op = self._operator()
        try:
            if path == "/set-operator":
                self._send("", 303, extra_headers=[
                    ("Location", form.get("back", "/")),
                    ("Set-Cookie", f"op={urllib.parse.quote(form.get('op', 'zhang'))}; Path=/")])
                return
            if path == "/scale/toggle":
                adapters.scale.set_online(not adapters.scale.online)
                return self._redirect(self._flash_path("/weigh",
                        f"地磅已{'上线' if adapters.scale.online else '中断（断磅样例）'}", None))
            if path == "/orders/new":
                return self.create_order(form, op)
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[0] == "orders":
                code, action = parts[1], parts[2]
                return self.order_action(code, action, form, op)
            if len(parts) == 3 and parts[0] == "reviews":
                return self.review_action(int(parts[1]), parts[2], form, op)
            self._send("404", 404, "text/plain; charset=utf-8")
        except store.AuditedError as exc:
            back = form.get("back", "/")
            self._redirect(self._flash_path(back, None, f"【{KIND_CN.get(exc.code, exc.code)}】{exc.message}"))
        except store.DomainError as exc:
            back = form.get("back", "/")
            self._redirect(self._flash_path(back, None, exc.message))

    # ------------------------------------------------ 布局
    def layout(self, title: str, content: str) -> str:
        op = self._operator()
        scale_online = adapters.scale.online
        opts = "".join(
            f"<option value='{esc(u['username'])}' "
            f"{'selected' if u['username'] == op['username'] else ''}>"
            f"{esc(u['display'])}（{ROLE_CN.get(u['role'], u['role'])}）</option>"
            for u in users())
        return f"""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{esc(title)} · 砂浆提货过磅</title>
<link rel='stylesheet' href='/static/style.css'></head><body>
<header class='topbar'>
  <div class='brand'>散装砂浆<span class='accent'>提货过磅</span>核对系统</div>
  <nav>
    <a href='/'>排队大屏</a><a href='/orders'>订单登记</a><a href='/weigh'>两次过磅</a>
    <a href='/silos'>料仓库存</a><a href='/exceptions'>异常留痕</a><a href='/reviews'>结算复核</a>
  </nav>
  <div class='spacer'></div>
  <span class='status-dot {'dot-on' if scale_online else 'dot-off'}'></span>
  <span class='small'>地磅{'在线' if scale_online else '中断'}</span>
  <form method='post' action='/set-operator' class='inline'>
    <input type='hidden' name='back' value='{esc(self.path)}'>
    <select name='op' onchange='this.form.submit()' style='background:#2f343a;color:#fff;border:1px solid #69717b;border-radius:4px;padding:4px'>{opts}</select>
  </form>
  <span class='fake'>车辆/金额均为虚构</span>
  <span class='clock' id='clock'></span>
</header>
<main class='wrap'>{content}</main>
<script>
function tick(){{let d=new Date();document.getElementById('clock').textContent=
 d.toLocaleTimeString('zh-CN',{{hour12:false}});}}
tick();setInterval(tick,1000);setTimeout(()=>location.reload(),15000);
</script></body></html>"""

    def flash(self, q: dict) -> str:
        if q.get("msg"):
            return f"<div class='flash ok'>{esc(q['msg'])}</div>"
        if q.get("err"):
            return f"<div class='flash err'>{esc(q['err'])}</div>"
        return ""

    # ------------------------------------------------ 排队大屏
    def page_board(self):
        q = self._query()
        rows = store.queue_view()
        cards = []
        for o in rows:
            tail4 = (o["plate"] or "----")[-4:]
            cards.append(f"""
<div class='card is-{esc(o['status'])}'>
  <div class='stage-line'>
    <span class='seq'>{esc(o['slot'])} · #{o['slot_seq']:02d} · {esc(o['code'].split('-')[-1])}</span>
    <span class='badge b-{esc(o['status'])}'>{esc(STATUS_CN[o['status']])}</span>
  </div>
  <div class='tail4'>{esc(tail4)}</div>
  <div class='plate-full'>{esc(o['plate'] or '未入场')}</div>
  <div class='model'>砂浆 {esc(o['model'])}</div>
  <div class='stage'>当前环节：{esc(STATUS_CN[o['status']])}</div>
  <div class='meta'>料仓 {esc(o['silo_id'])} · 订单 {o['qty_kg']}kg · <a href='/orders/{esc(o['code'])}' style='color:#cfe0ff'>作业台→</a></div>
</div>""")
        body = f"""
{self.flash(q)}
<h1>交付预约排队屏</h1>
<div class='panel navy'>
  <div class='board'>{''.join(cards) or '<p>当前没有在场车辆</p>'}</div>
</div>
<h2>料仓料位（直立刻度）</h2>
<div class='panel'>{self.silo_gauges()}</div>
<p class='small'>页面每 15 秒自动刷新；车牌后四位、砂浆型号、当前环节为大屏三要素。</p>"""
        self._send(self.layout("排队大屏", body))

    def silo_gauges(self) -> str:
        out = ["<div class='silo-row'>"]
        for s in store.silo_view():
            cap = s["capacity_kg"]
            level = s["stock_kg"] / cap * 100
            locked_pct = (s["locked_kg"] or 0) / cap * 100
            ticks = []
            for frac in (0, .25, .5, .75, 1.0):
                kg = int(cap * frac)
                ticks.append(
                    f"<span class='tick' style='bottom:{frac*100:.1f}%;width:{8 if frac in (0,.5,1) else 5}px'></span>"
                    f"<span class='ticklabel' style='bottom:calc({frac*100:.1f}% - 6px)'>{kg//1000}t</span>")
            out.append(f"""
<div class='silo'>
  <div class='tube'>
    <div class='fill' style='height:{level:.1f}%'></div>
    <div class='fill locked-overlay' style='height:{locked_pct:.1f}%'></div>
    <div class='ticks'>{''.join(ticks)}</div>
  </div>
  <div class='label'>料仓 {esc(s['id'])}</div>
  <div class='model-tag'>{esc(s['model'])}</div>
  <div class='sub'>库存 {s['stock_kg']}kg</div>
  <div class='sub' style='color:#9db8e0'>已锁 {s['locked_kg']}kg</div>
</div>""")
        out.append("</div>")
        return "".join(out)

    def page_silos(self):
        body = f"<h1>料仓库存</h1><div class='panel'>{self.silo_gauges()}</div>"
        rows = get_db().execute(
            "SELECT m.*, o.code FROM stock_movements m LEFT JOIN orders o ON o.code=m.order_code "
            "ORDER BY m.id DESC LIMIT 30").fetchall()
        tr = "".join(
            f"<tr><td>{esc(r['created_at'])}</td><td>{esc(r['silo_id'])}</td>"
            f"<td>{esc(r['kind'])}</td><td class='num'>{r['change_kg']}</td>"
            f"<td class='mono'>{esc(r['code'] or '')}</td></tr>" for r in rows)
        body += f"""<h2>库存变动流水（近 30 条）</h2><div class='panel'><table>
<tr><th>时间</th><th>料仓</th><th>类型</th><th class='num'>变化kg</th><th>订单</th></tr>{tr}</table></div>"""
        self._send(self.layout("料仓库存", body))

    # ------------------------------------------------ 订单列表 / 登记
    def page_orders(self):
        q = self._query()
        rows = get_db().execute("SELECT * FROM orders ORDER BY id DESC").fetchall()
        tr = []
        for o in rows:
            tr.append(f"""<tr>
<td class='mono'><a href='/orders/{esc(o['code'])}'>{esc(o['code'])}</a></td>
<td>{esc(o['customer_code'])}</td><td>{esc(o['model'])}</td><td>{esc(o['silo_id'])}</td>
<td>{esc(o['slot'])} #{o['slot_seq']:02d}</td>
<td class='mono'>{esc(o['plate'] or '—')}</td>
<td class='num'>{o['qty_kg']}</td>
<td><span class='badge b-{esc(o['status'])}'>{esc(STATUS_CN[o['status']])}</span></td>
<td class='num'>{'' if o['net_kg'] is None else f"{o['net_kg']:.1f}"}</td>
<td class='num'>{'' if o['settle_amount'] is None else f"¥{o['settle_amount']:,.2f}"}</td>
</tr>""")
        body = f"""
{self.flash(q)}
<div style='display:flex;justify-content:space-between;align-items:center'>
<h1 style='margin:0'>订单与提货</h1>
<a class='btn navy' href='/orders/new'>＋ 登记新订单（付款确认）</a></div>
<div class='panel' style='margin-top:14px'><table>
<tr><th>订单号</th><th>客户代号</th><th>型号</th><th>料仓</th><th>预约</th>
<th>车牌</th><th class='num'>订单kg</th><th>状态</th><th class='num'>净重</th><th class='num'>结算</th></tr>
{''.join(tr)}</table></div>"""
        self._send(self.layout("订单", body))

    def page_new_order(self):
        customers = get_db().execute("SELECT * FROM customers").fetchall()
        silos = get_db().execute("SELECT * FROM silos ORDER BY id").fetchall()
        prices = get_db().execute("SELECT DISTINCT version FROM prices").fetchall()
        c_opts = "".join(f"<option value='{esc(c['code'])}'>{esc(c['code'])} {esc(c['name'])}</option>" for c in customers)
        s_opts = "".join(f"<option value='{esc(s['id'])}'>{esc(s['id'])} 仓（{esc(s['model'])}）</option>" for s in silos)
        p_opts = "".join(f"<option>{esc(p['version'])}</option>" for p in prices)
        body = f"""
<h1>登记订单 · 付款确认后入提货队列</h1>
<div class='panel navy'><form method='post' action='/orders/new'>
<label class='f'>客户代号 <select name='customer_code'>{c_opts}</select></label>
<label class='f'>砂浆型号 <select name='model' id='model'>
  <option>M5</option><option selected>M7.5</option><option>M10</option><option>M15</option></select></label>
<label class='f'>料仓 <select name='silo_id'>{s_opts}</select> <span class='small'>须与型号一致，错选将留痕</span></label>
<label class='f'>订单数量（干料kg） <input type='number' name='qty_kg' value='2000' min='100' step='100'></label>
<label class='f'>预约时段 <input type='time' name='slot' value='10:30'></label>
<label class='f'>车型 <select name='vehicle_type'>
  <option>两轴罐车</option><option selected>三轴罐车</option><option>牵引罐车</option></select></label>
<label class='f'>价格版本 <select name='price_version'>{p_opts}</select></label>
<button type='submit'>确认付款并入队</button>
<a class='btn gray' href='/orders'>返回</a>
<p class='small'>提交后模拟付款渠道返回虚构回执，订单状态置为“已付款待入场”。</p>
</form></div>"""
        self._send(self.layout("登记订单", body))

    def create_order(self, form, op):
        code = store.create_order(
            customer_code=form["customer_code"], model=form["model"],
            silo_id=form["silo_id"], qty_kg=int(float(form["qty_kg"])),
            slot=form["slot"], vehicle_type=form["vehicle_type"],
            price_version=form["price_version"], operator=op["username"])
        self._redirect(f"/orders/{code}?msg=" + urllib.parse.quote(f"订单 {code} 已付款并入队（虚构回执）"))

    # ------------------------------------------------ 过磅页
    def page_weigh(self):
        q = self._query()
        rows = get_db().execute(
            "SELECT * FROM orders WHERE status IN ('QUEUED','TARE','LOADING','LOADED','WEIGHED') "
            "ORDER BY slot,slot_seq").fetchall()
        blocks = []
        for o in rows:
            blocks.append(self.weigh_block(o))
        toolbar = f"""
<div class='toolbar'>
<form method='post' action='/scale/toggle' class='inline'>
<button class='{'ghost' if adapters.scale.online else 'danger'}'>
{'模拟地磅中断（断磅）' if adapters.scale.online else '恢复地磅上线'}</button></form>
<span class='small'>地磅适配器在线状态用于演示“地磅中断”留痕</span></div>"""
        body = f"{self.flash(q)}<h1>两次过磅作业</h1>{toolbar}<div class='grid2'>{''.join(blocks)}</div>"
        self._send(self.layout("两次过磅", body))

    def weigh_stack(self, o) -> str:
        def row(cls, tag, val, unit="kg"):
            v = "—" if val is None else f"{val:,.1f}"
            return (f"<div class='weigh-row {cls}'><span class='tag'>{tag}</span>"
                    f"<span></span><span class='val'>{v}<span class='unit'>{unit}</span></span></div>")
        return "<div class='weigh-stack'>" + row("muted", "皮 重", o["tare_kg"]) + \
            row("muted", "毛 重", o["gross_kg"]) + row("net", "净 重", o["net_kg"]) + "</div>"

    def weigh_block(self, o) -> str:
        forms = []
        back = f"/orders/{o['code']}"
        if o["status"] == "QUEUED":
            forms.append(f"""
<form method='post' action='/orders/{esc(o['code'])}/tare'>
<input type='hidden' name='back' value='/weigh'>
<button class='navy'>读取首次过磅（皮重）并锁库存</button></form>""")
        if o["status"] in ("TARE", "LOADING", "LOADED", "WEIGHED"):
            remain = store.remaining_qty(get_db(), o)
            forms.append(f"""
<form method='post' action='/orders/{esc(o['code'])}/load'>
<input type='hidden' name='back' value='/weigh'>
<label class='f'>料仓 <select name='silo_id'>
  <option {'' if o['silo_id']!='A' else 'selected'}>A</option>
  <option {'' if o['silo_id']!='B' else 'selected'}>B</option>
  <option {'' if o['silo_id']!='C' else 'selected'}>C</option>
</select> 湿料kg <input type='number' name='wet_kg' value='{max(0,int(remain))}' step='50'>
 含水% <input type='number' name='moisture_pct' value='{o['moisture_pct'] or 0}' step='0.5' min='0' max='15' style='width:80px'>
<button>装料/补装</button></label></form>
<p class='small'>订单余量（折干） {remain:.1f}kg · 本单已锁 {o['locked_stock_kg']}kg
{'· 已进入补装阶段' if o['status'] in ('LOADED','WEIGHED') else ''}</p>""")
        if o["status"] in ("TARE", "LOADING"):
            forms.append(f"""
<form method='post' action='/orders/{esc(o['code'])}/finish-load'>
<input type='hidden' name='back' value='/weigh'><button class='gray'>装料完成，前往复磅</button></form>""")
        if o["status"] in ("LOADED", "WEIGHED"):
            forms.append(f"""
<form method='post' action='/orders/{esc(o['code'])}/gross'>
<input type='hidden' name='back' value='/weigh'>
<button class='navy'>读取二次过磅（毛重）</button>
<label class='f'>含水争议 <label><input type='checkbox' name='disputed' value='1'> 客户对含水修正有异议</label></label>
</form>""")
        return f"""<div class='panel'>
<div style='display:flex;justify-content:space-between;align-items:center'>
<a href='/orders/{esc(o['code'])}'><strong>{esc(o['plate'])}</strong> · {esc(o['model'])}</a>
<span class='badge b-{esc(o['status'])}'>{esc(STATUS_CN[o['status']])}</span></div>
<div style='margin:10px 0'>{self.weigh_stack(o)}</div>
{''.join(forms)}</div>"""

    # ------------------------------------------------ 订单作业台
    def page_order(self, code: str):
        q = self._query()
        o = store.get_order(code)
        if o is None:
            return self._send("订单不存在", 404, "text/plain; charset=utf-8")
        events = store.events_of(code)
        excs = get_db().execute(
            "SELECT * FROM exceptions WHERE order_code=? ORDER BY id", (code,)).fetchall()
        reviews = get_db().execute(
            "SELECT * FROM reviews WHERE order_code=? ORDER BY id", (code,)).fetchall()

        # 操作按钮按状态出现
        actions = self.order_forms(o)

        # 重量事件时间线（人工改重紧邻原值）
        li = []
        for e in events:
            prev = ""
            if e["prev_value_kg"] is not None:
                prev = (f"<span class='strike'>原值 {e['prev_value_kg']:.1f}kg</span> → ")
            moisture = f" · 含水 {e['moisture_pct']}%" if e["moisture_pct"] is not None else ""
            rev = f"<div class='rev'>复核：{esc(e['reviewer'])}</div>" if e["reviewer"] else ""
            li.append(f"""<li class='ev-{esc(e['kind'])}'>
<span class='ev-kind'>{esc(EVENT_CN.get(e['kind'], e['kind']))}</span>
 {prev}<strong>{e['value_kg']:.1f}kg</strong>{esc(moisture)}
 <span class='small'>{esc(e['created_at'])} · {esc(e['operator'])}</span>
 <div class='small'>{esc(e['note'])}</div>{rev}</li>""")

        exc_html = []
        for x in excs:
            exc_html.append(f"""<tr class='{'resolved' if x['resolved'] else ''}'>
<td>{esc(x['created_at'])}</td><td><span class='kind k-{esc(x['kind'])}'>{esc(KIND_CN.get(x['kind'], x['kind']))}</span></td>
<td>{esc(x['detail'])}</td><td>{esc(x['operator'])}</td></tr>""")

        review_html = []
        for r in reviews:
            review_html.append(f"""<tr class='{'resolved' if r['status']!='PENDING' else ''}'>
<td>#{r['id']}</td><td>{esc(r['kind'])}</td><td class='mono'>{esc(r['payload'])}</td>
<td>{esc(r['requested_by'])}（{esc(ROLE_CN.get(r['request_role'], r['request_role']))}）</td>
<td>{esc(r['status'])}</td><td>{esc(r['reviewed_by'] or '')}</td></tr>""")

        vehicle_img = f"/img/vehicles/{urllib.parse.quote(o['plate'])}.svg" if o["plate"] else ""
        slips = get_db().execute(
            "SELECT note FROM weight_events WHERE order_code=? AND (kind='TARE' OR kind='GROSS') "
            "AND note LIKE '票号%'", (code,)).fetchall()
        slip_imgs = []
        for s in slips:
            note = s["note"]
            ticket = note.split("票号", 1)[1].strip().split("；", 1)[0].strip()
            kind = "皮重" if "皮重" in note else "毛重"
            slip_imgs.append(
                f"<img class='thumb' src='/img/slips/{urllib.parse.quote(ticket)}.svg' "
                f'alt=\'{esc(ticket)}\'>')

        kv = f"""<dl class='kv'>
<dt>订单号</dt><dd>{esc(o['code'])}</dd>
<dt>客户代号</dt><dd>{esc(o['customer_code'])}</dd>
<dt>砂浆型号</dt><dd>{esc(o['model'])}</dd>
<dt>料仓</dt><dd>{esc(o['silo_id'])}</dd>
<dt>预约时段</dt><dd>{esc(o['slot'])}（序号 #{o['slot_seq']}）</dd>
<dt>车型/车牌</dt><dd>{esc(o['vehicle_type'])} / {esc(o['plate'] or '未入场')}</dd>
<dt>订单数量</dt><dd>{o['qty_kg']} kg（折干）</dd>
<dt>已装湿料</dt><dd>{o['load_kg'] or 0} kg（含水 {o['moisture_pct'] or 0}%）· 补装累计 {o['refilled_qty_kg']} kg</dd>
<dt>价格版本</dt><dd>{esc(o['price_version'])} @ ¥{o['unit_price']:.2f}/kg</dd>
<dt>已付款</dt><dd>¥{o['paid_amount']:,.2f} <span class='small'>{esc(o['pay_receipt'])}</span></dd>
<dt>结算快照</dt><dd>{'—' if o['settle_amount'] is None else f'¥{o["settle_amount"]:,.2f}'}（退款累计 ¥{o['refunded_amount']:,.2f}）</dd>
<dt>状态</dt><dd><span class='badge b-{esc(o['status'])}'>{esc(STATUS_CN[o['status']])}</span></dd>
</dl>"""

        body = f"""
{self.flash(q)}
<div style='display:flex;justify-content:space-between;align-items:center'>
<h1 style='margin:0'>作业台 · {esc(o['plate'] or '未入场')} · {esc(o['model'])}</h1>
<a class='btn gray' href='/orders'>← 返回列表</a></div>
<div class='grid2' style='margin-top:14px'>
  <div class='panel'><h2 style='margin-top:0'>订单与结算</h2>{kv}
    <h2>过磅三联</h2>{self.weigh_stack(o)}</div>
  <div class='panel navy'><h2 style='margin-top:0'>作业操作</h2>{actions}
    <p class='small'>提示：已放行车辆不能重开装料；补装不得超过订单余量；改重与退款须另一角色复核。</p></div>
</div>
<div class='grid2'>
  <div class='panel'><h2 style='margin-top:0'>车辆与磅单（本地图片，虚构）</h2>
    {f"<img class='thumb' src='{vehicle_img}'>" if vehicle_img else "<p class='small'>车辆入场后生成</p>"}
    <div style='display:flex;gap:10px;flex-wrap:wrap;margin-top:10px'>{''.join(slip_imgs)}</div></div>
  <div class='panel'><h2 style='margin-top:0'>重量事件审计（修订紧邻原值）</h2>
    <ul class='timeline'>{''.join(li) or '<li class="small">暂无</li>'}</ul></div>
</div>
<h2>异常留痕</h2>
<div class='panel'><table><tr><th>时间</th><th>类型</th><th>详情</th><th>操作人</th></tr>
{''.join(exc_html) or '<tr><td colspan=4 class=small>无异常</td></tr>'}</table></div>
<h2>复核记录（人工改重 / 退款）</h2>
<div class='panel'><table><tr><th>#</th><th>类型</th><th>内容</th><th>申请</th><th>状态</th><th>复核</th></tr>
{''.join(review_html) or '<tr><td colspan=6 class=small>暂无</td></tr>'}</table></div>"""
        self._send(self.layout(o["code"], body))

    def order_forms(self, o) -> str:
        code = o["code"]
        back = f"/orders/{code}"
        f = []
        if o["status"] == "PAID":
            f.append(f"""<form method='post' action='/orders/{esc(code)}/checkin'>
<label class='f'>车牌 <input name='plate' value='沪B·' maxlength='10' required>
到场车型 <select name='vehicle_type'>
  <option {'selected' if o['vehicle_type']=='两轴罐车' else ''}>两轴罐车</option>
  <option {'selected' if o['vehicle_type']=='三轴罐车' else ''}>三轴罐车</option>
  <option {'selected' if o['vehicle_type']=='牵引罐车' else ''}>牵引罐车</option></select>
<button>车辆入场并入队</button></label></form>""")
        if o["status"] == "QUEUED":
            f.append(f"""<form method='post' action='/orders/{esc(code)}/tare'>
<button class='navy'>首次过磅：读皮重并锁定订单库存</button></form>""")
            tare_exc = get_db().execute(
                "SELECT 1 FROM exceptions WHERE order_code=? AND kind='TARE_ABNORMAL' AND resolved=0",
                (code,)).fetchone()
            if tare_exc:
                f.append(f"""<form method='post' action='/orders/{esc(code)}/tare'>
<input type='hidden' name='force' value='1'>
<button class='danger'>主管强制采纳异常皮重（切换到孙主管后点击）</button>
<p class='small'>地磅偏差导致皮重超出车型合理区间，须主管确认后采纳并继续。</p></form>""")
        if o["status"] in ("TARE", "LOADING", "LOADED", "WEIGHED") and o["status"] != "RELEASED":
            remain = store.remaining_qty(get_db(), o)
            f.append(f"""<details><summary class='btn' style='display:inline-block'>装料 / 补装（余量 {remain:.0f}kg）</summary>
<form method='post' action='/orders/{esc(code)}/load' style='margin-top:8px'>
<label class='f'>料仓 <select name='silo_id'><option>{esc(o['silo_id'])}</option>
  <option>A</option><option>B</option><option>C</option></select>
 湿料kg <input type='number' name='wet_kg' value='{max(100,int(remain))}' step='50'>
 含水% <input type='number' name='moisture_pct' value='{o['moisture_pct'] or 0}' step='.5' style='width:80px'></label>
<button>{'补装（订单余量内）' if o['status'] in ('LOADED','WEIGHED') else '开始装料'}</button>
<p class='small'>选错误仓会触发“料仓错选”留痕；超量/补装超单将被阻止并留痕。</p></form></details>""")
        if o["status"] in ("TARE", "LOADING"):
            f.append(f"""<form method='post' action='/orders/{esc(code)}/finish-load'>
<button class='gray'>装料完成</button></form>""")
        if o["status"] in ("LOADED", "WEIGHED"):
            f.append(f"""<form method='post' action='/orders/{esc(code)}/gross'>
<button class='navy'>二次过磅：读毛重、算净重</button>
<label class='f'><input type='checkbox' name='disputed' value='1'> 含水修正有争议（留痕待主管裁定）</label></form>""")
        # 含水争议裁定（主管）
        if o["status"] == "WEIGHED":
            has_disp = get_db().execute(
                "SELECT 1 FROM exceptions WHERE order_code=? AND kind='MOISTURE_DISPUTE' AND resolved=0",
                (code,)).fetchone()
            if has_disp:
                f.append(f"""<details><summary class='btn danger'>主管裁定含水争议</summary>
<form method='post' action='/orders/{esc(code)}/adjudicate' style='margin-top:8px'>
<label class='f'>裁定含水% <input type='number' name='moisture_pct' value='{o['moisture_pct'] or 0}' step='.5' style='width:90px'></label>
<button class='danger'>按裁定值重算净重</button></form></details>""")
        if o["status"] in ("WEIGHED", "REJECTED"):
            f.append(f"""<div class='toolbar'>
<form method='post' action='/orders/{esc(code)}/confirm' class='inline'>
<input type='hidden' name='sign' value='1'><button>客户确认重量，生成结算快照</button></form>
<form method='post' action='/orders/{esc(code)}/confirm' class='inline'>
<input type='hidden' name='sign' value='0'><button class='danger'>客户拒签</button>
<input name='reason' placeholder='拒签原因' style='background:#2f343a;color:#fff;border:1px solid #69717b;border-radius:4px;padding:6px'></form></div>""")
        if o["status"] == "CONFIRMED":
            f.append(f"""<form method='post' action='/orders/{esc(code)}/release'>
<button style='background:#3f9d5b'>出场核验通过，放行</button></form>""")
        # 人工改重申请
        if o["status"] in ("TARE", "LOADING", "LOADED", "WEIGHED", "CONFIRMED", "REJECTED"):
            f.append(f"""<details><summary class='btn ghost'>人工改重申请（需另一角色复核）</summary>
<form method='post' action='/orders/{esc(code)}/manual-weight' style='margin-top:8px'>
<label class='f'>改哪一项 <select name='field'><option value='tare'>皮重</option>
  <option value='gross'>毛重</option></select> 新值kg <input type='number' name='new_value' step='.1' required></label>
<label class='f'>原因 <input name='reason' style='width:70%' placeholder='如：地磅校准偏差'></label>
<button class='ghost'>提交改重申请</button></form></details>""")
        # 退款
        if o["status"] in ("WEIGHED", "CONFIRMED", "REJECTED", "RELEASED"):
            refundable = (o["settle_amount"] or o["paid_amount"]) - (o["refunded_amount"] or 0)
            f.append(f"""<details><summary class='btn danger'>退款（可退 ¥{refundable:,.2f}，需主管复核）</summary>
<form method='post' action='/orders/{esc(code)}/refund' style='margin-top:8px'>
<label class='f'>退款金额 <input type='number' name='amount' value='{refundable:.2f}' step='.01' style='width:130px'>
原因 <input name='reason' style='width:50%' placeholder='如：短重退差/取消订单'></label>
<button class='danger'>发起退款申请（幂等）</button></form></details>""")
        return "".join(f) or "<p class='small'>当前状态没有可执行操作。</p>"

    def order_action(self, code, action, form, op):
        back = form.get("back", f"/orders/{code}")
        if action == "checkin":
            store.check_in(code, form["plate"], form["vehicle_type"], op["username"])
            return self._redirect(self._flash_path(back, f"车辆 {form['plate']} 已入场并入队", None))
        if action == "tare":
            force = bool(form.get("force"))
            if force and op["role"] != "supervisor":
                raise store.DomainError("ROLE_CONFLICT", "皮重异常的强制采纳须主管身份")
            r = store.weigh_tare(code, op["username"], force=force)
            return self._redirect(self._flash_path(back,
                f"皮重 {r['tare_kg']:.0f}kg，锁定库存 {r['locked_kg']}kg，票号 {r['ticket']}"
                + ("（主管强制采纳）" if force else ""), None))
        if action == "load":
            r = store.load_material(code, form["silo_id"], float(form["wet_kg"]),
                                    float(form.get("moisture_pct") or 0), op["username"])
            label = "补装" if get_db().execute(
                "SELECT 1 FROM weight_events WHERE order_code=? AND kind='REFILL'", (code,)).fetchone() else "装料"
            return self._redirect(self._flash_path(back,
                f"{label}湿料 {r['wet_kg']:.0f}kg，折干 {r['dry_kg']:.1f}kg，余量 {r['remaining']:.1f}kg", None))
        if action == "finish-load":
            store.finish_loading(code, op["username"])
            return self._redirect(self._flash_path(back, "装料完成，可二次过磅", None))
        if action == "gross":
            r = store.weigh_gross(code, disputed=bool(form.get("disputed")), operator=op["username"])
            msg = f"毛重 {r['gross_kg']:.0f}kg，净重 {r['net_kg']:.1f}kg（含水 {r['moisture_pct']}%）"
            if form.get("disputed"):
                msg += "；含水争议已留痕，待主管裁定"
            return self._redirect(self._flash_path(back, msg, None))
        if action == "adjudicate":
            r = store.adjudicate_moisture(code, float(form["moisture_pct"]), op["username"])
            return self._redirect(self._flash_path(back,
                f"主管裁定含水 {r['moisture_pct']}%，净重 {r['net_kg']:.1f}kg", None))
        if action == "confirm":
            sign = form.get("sign") == "1"
            store.customer_confirm(code, sign, op["username"], form.get("reason", ""))
            if sign:
                return self._redirect(self._flash_path(back, "客户已确认重量，结算快照已生成", None))
            return self._redirect(self._flash_path(back, None, "客户拒签，已挂起并留痕"))
        if action == "release":
            store.release_gate(code, op["username"])
            return self._redirect(self._flash_path(back, "出场核验通过，已放行（不可逆）", None))
        if action == "manual-weight":
            rid = store.request_manual_weight(code, form["field"], float(form["new_value"]),
                                              op["username"], form.get("reason", ""))
            return self._redirect("/reviews?msg=" + urllib.parse.quote(f"改重申请 #{rid} 已提交，待另一角色复核"))
        if action == "refund":
            rid = store.request_refund(code, float(form["amount"]), op["username"], form.get("reason", ""))
            return self._redirect("/reviews?msg=" + urllib.parse.quote(f"退款申请 #{rid} 已提交（幂等键 REFUND:{code}）"))
        self._redirect(back)

    # ------------------------------------------------ 异常 / 复核台
    def page_exceptions(self):
        q = self._query()
        rows = get_db().execute("SELECT * FROM exceptions ORDER BY id DESC").fetchall()
        tr = []
        for x in rows:
            tr.append(f"""<tr class='{'resolved' if x['resolved'] else ''}'>
<td>{esc(x['created_at'])}</td>
<td class='mono'><a href='/orders/{esc(x['order_code'] or '')}'>{esc(x['order_code'] or '')}</a></td>
<td class='mono'>{esc(x['plate'] or '')}</td>
<td><span class='kind k-{esc(x['kind'])}'>{esc(KIND_CN.get(x['kind'], x['kind']))}</span></td>
<td>{esc(x['detail'])}</td><td>{esc(x['operator'])}</td></tr>""")
        body = f"""{self.flash(q)}
<h1>异常留痕</h1>
<div class='panel'><table><tr><th>时间</th><th>订单</th><th>车牌</th><th>类型</th><th>详情</th><th>操作人</th></tr>
{''.join(tr) or '<tr><td colspan=6>无异常</td></tr>'}</table></div>
<p class='small'>八类异常：车型不符、料仓错选、皮重异常、装料超量、地磅中断、含水争议、补装后超单、客户拒签（另含过号释放）。</p>"""
        self._send(self.layout("异常留痕", body))

    def page_reviews(self):
        q = self._query()
        op = self._operator()
        rows = get_db().execute("SELECT * FROM reviews ORDER BY id DESC").fetchall()
        tr = []
        for r in rows:
            approve = ""
            if r["status"] == "PENDING":
                act = "approve-weight" if r["kind"] == "MANUAL_WEIGHT" else "approve-refund"
                approve = (f"<form method='post' action='/reviews/{r['id']}/{act}' class='inline'>"
                           f"<button class='{'navy' if 'weight' in act else 'danger'}'>"
                           f"以当前身份（{esc(ROLE_CN.get(op['role'], op['role']))}）复核通过</button></form>")
            tr.append(f"""<tr class='{'resolved' if r['status']!='PENDING' else ''}'>
<td>#{r['id']}</td>
<td class='mono'><a href='/orders/{esc(r['order_code'])}'>{esc(r['order_code'])}</a></td>
<td>{esc(r['kind'])}</td><td class='mono'>{esc(r['payload'])}</td>
<td>{esc(r['requested_by'])}<br><span class='small'>{esc(ROLE_CN.get(r['request_role'], r['request_role']))}</span></td>
<td>{esc(r['status'])}</td><td>{esc(r['reviewed_by'] or '—')}<br><span class='small'>{esc(r['comment'])}</span></td>
<td>{approve}</td></tr>""")
        body = f"""{self.flash(q)}
<h1>结算复核台</h1>
<div class='flash info'>当前身份：<strong>{esc(op['display'])}（{esc(ROLE_CN.get(op['role'], op['role']))}）</strong>。
人工改重与退款必须由<span style='color:#ffd45e'>不同角色的第二人</span>复核——用顶栏切换身份即可演示角色冲突；
退款按订单幂等，重复提交/重复审批不会二次打款。</div>
<div class='panel'><table><tr><th>#</th><th>订单</th><th>类型</th><th>内容</th><th>申请</th>
<th>状态</th><th>复核/回执</th><th>操作</th></tr>{''.join(tr) or '<tr><td colspan=8>暂无</td></tr>'}</table></div>"""
        self._send(self.layout("结算复核", body))

    def review_action(self, rid: int, action: str, form, op):
        if action == "approve-weight":
            store.approve_manual_weight(rid, op["username"])
            return self._redirect("/reviews?msg=" + urllib.parse.quote(f"#{rid} 人工改重已复核生效，原值保留于审计时间线"))
        if action == "approve-refund":
            r = store.approve_refund(rid, op["username"])
            if r.get("idempotent"):
                return self._redirect("/reviews?msg=" + urllib.parse.quote(
                    f"#{rid} 为重复审批，幂等返回原回执 {r['receipt']}，未重复打款"))
            return self._redirect("/reviews?msg=" + urllib.parse.quote(
                f"#{rid} 退款已复核执行 ¥{r['amount']:.2f}，回执 {r['receipt']}（虚构）"))
        self._redirect("/reviews")

    # ------------------------------------------------ 静态资源
    def serve_css(self):
        data = Path("app/static/style.css").read_bytes()
        self._send(data, 200, "text/css; charset=utf-8")

    def static_image(self, rel: str):
        # 防目录穿越（先解码百分号编码，如中文车牌）
        rel = urllib.parse.unquote(rel)
        root = Path("data/images").resolve()
        target = (root / rel).resolve()
        if root not in target.parents or not target.exists():
            return self._send("not found", 404, "text/plain; charset=utf-8")
        self._send(target.read_bytes(), 200, "image/svg+xml")


def serve(host="127.0.0.1", port=8000, reset: bool = False) -> None:
    if reset or not Path("data/app.db").exists():
        seed.seed("data/app.db", reset=True)
    else:
        init_db("data/app.db")
    scheduler.start(30)
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"散装砂浆提货过磅系统已启动：http://{host}:{port}  （Ctrl+C 停止）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        scheduler.stop()
