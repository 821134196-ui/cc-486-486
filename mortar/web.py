"""本地 Web 界面：排队屏、过磅页、料仓刻度、复核台。

仅用标准库 http.server；角色由 cookie 切换（本地虚构演示，不做真实鉴权）。
"""
from __future__ import annotations

import html
import json
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.cookies import SimpleCookie
from pathlib import Path

from . import adapters, services as svc
from .config import (
    GRACE_SECONDS,
    HOST,
    PORT,
    ROLE_NAMES,
    ROLE_CUSTOMER,
    ROLE_DISPATCHER,
    ROLE_FINANCE,
    ROLE_FINANCE_MGR,
    ROLE_OPERATOR,
    ROLE_SUPERVISOR,
    SWEEP_INTERVAL,
)
from .db import get_conn
from .seed import seed

STATIC = Path(__file__).parent / "static"

INCIDENT_CN = {
    "vehicle_mismatch": "车型不符",
    "silo_mismatch": "料仓错选",
    "tare_abnormal": "皮重异常",
    "overload": "装料超量",
    "scale_outage": "地磅中断",
    "moisture_dispute": "含水争议",
    "over_order_after_topup": "补装超单",
    "customer_reject": "客户拒签",
    "no_show": "过号释放",
}
ROUND_CN = {
    "tared": "已过皮重·待选仓装料",
    "loading": "装料中·待二次过磅",
    "loaded": "已过磅·待客户确认",
    "disputed": "含水争议·待主管复核",
    "rejected": "客户拒签·闸门冻结",
    "signed": "客户已签收",
    "extra_pending": "补装待装料",
}
ORDER_CN = {
    "pending_payment": "待付款",
    "queued": "排队中",
    "serving": "提货中",
    "completed": "已出场",
    "cancelled": "已取消",
}


def esc(x) -> str:
    return html.escape("" if x is None else str(x))


def plate_tail4(plate: str) -> str:
    digits = "".join(ch for ch in plate if ch.isalnum())
    return digits[-4:] if digits else plate[-4:]


# ================================================================ 页面片段

def shell(title: str, body: str, role: str, flash="", err="") -> str:
    role_opts = "".join(
        f'<option value="{r}"{" selected" if r == role else ""}>{esc(n)}</option>'
        for r, n in ROLE_NAMES.items()
    )
    scale_state = ("中断中" if adapters.scale.outage else "正常")
    scale_cls = "red" if adapters.scale.outage else "ok"
    return f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · 散装砂浆提货过磅</title>
<link rel="stylesheet" href="/static/style.css">
</head><body>
<div class="topbar">
  <div class="brand"><span class="bar">▌</span> 散装砂浆提货与过磅核对系统</div>
  <nav>
    <a href="/">排队屏</a><a href="/review">复核台</a>
    <a href="/incidents">异常留痕</a><a href="/control">模拟控制台</a>
  </nav>
  <div class="rolebox">
    <span class="badge {scale_cls}" style="font-size:12px">地磅：{scale_state}</span>
    <form method="post" action="/set-role" class="inline">
      <input type="hidden" name="back" value="">
      当前角色
      <select name="role" onchange="this.form.submit()">{role_opts}</select>
    </form>
  </div>
</div>
<div class="wrap">
  {f'<div class="flash ok">{esc(flash)}</div>' if flash else ''}
  {f'<div class="flash err">{esc(err)}</div>' if err else ''}
  {body}
  <p class="muted" style="margin-top:24px">车辆、金额与地磅数据均为虚构演示数据。重量事件与审计记录保存在本地 SQLite。</p>
</div>
</body></html>"""


def silo_panel() -> str:
    conn = svc._conn()
    silos = conn.execute(
        "SELECT s.*, p.name pname FROM silos s JOIN products p ON p.code=s.product_code"
        " ORDER BY s.code"
    ).fetchall()
    conn.close()
    items = []
    for s in silos:
        pct = max(0.0, min(1.0, s["stock"] / s["capacity"]))
        low = pct < 0.2
        items.append(f"""
        <div class="silo">
          <div class="tube"><div class="fill{' low' if low else ''}"
               style="height:{pct * 100:.0f}%"></div>
            <div class="num">{s['stock']:.1f}t</div></div>
          <div class="code">{esc(s['code'])}</div>
          <div class="meta">{esc(s['product_code'])}<br>仓容 {s['capacity']:.0f}t</div>
        </div>""")
    return f'<div class="card"><h3>料仓库存（直立刻度）</h3><div class="silo-row">{"".join(items)}</div></div>'


def order_card(o: dict) -> str:
    tail = plate_tail4(o["active_plate"] or o["expected_plate"] or "----")
    full = esc(o["active_plate"] or o["expected_plate"] or "车辆未入场")
    if o["status"] == "queued":
        if o["called_at"]:
            stage, cls = "呼号中·等待入场", "call"
        else:
            stage, cls = "排队等候", "wait"
    elif o["status"] == "serving":
        stage = ROUND_CN.get(o["round_status"], "提货中")
        cls = "bad" if o["round_status"] in ("disputed", "rejected") or _has_open(o) else "ok"
    else:
        stage, cls = ORDER_CN.get(o["status"], o["status"]), "gray"
    badges = []
    for i in _open_incidents(o["id"]):
        badges.append(f'<span class="badge red">{INCIDENT_CN.get(i["code"], i["code"])}</span>')
    if o["no_show"]:
        badges.append(f'<span class="badge yellow">过号 {o["no_show"]} 次</span>')
    if o["rounds"]:
        badges.append(f'<span class="badge gray">第 {o["rounds"]} 轮</span>')
    return f"""
    <a class="card board-card {'serving' if o['status']=='serving' else 'warn' if badges else ''}"
       href="/order/{o['id']}" style="text-decoration:none;color:inherit">
      <div class="plate4">{esc(tail)}<span class="full">{full}</span></div>
      <div class="pcode">{esc(o['product_code'])}</div>
      <div style="font-size:13px;color:var(--cement-700)">{esc(o['order_no'])} · {esc(o['customer_name'])}</div>
      <div style="margin-top:6px"><span class="stage {cls}">{esc(stage)}</span></div>
      <div class="badges">{''.join(badges)}</div>
      <div class="muted" style="margin-top:4px">预约 {esc((o['slot_start'] or '')[-11:])} · 订购 {o['qty_ordered']:.1f}t</div>
    </a>"""


def _has_open(o):
    return bool(_open_incidents(o["id"]))


def _open_incidents(order_id):
    conn = svc._conn()
    rows = conn.execute(
        "SELECT code FROM incidents WHERE order_id=? AND status='open'", (order_id,)
    ).fetchall()
    conn.close()
    return rows


def board_page(role: str) -> str:
    board = svc.queue_board()
    conn = svc._conn()
    products = conn.execute("SELECT code,name FROM products ORDER BY code").fetchall()
    customers = conn.execute("SELECT code,name FROM customers ORDER BY code").fetchall()
    vehicles = conn.execute("SELECT * FROM vehicles ORDER BY plate").fetchall()
    done = conn.execute(
        "SELECT o.order_no, o.id, s.plate, s.total_net, s.amount FROM settlements s"
        " JOIN orders o ON o.id=s.order_id ORDER BY s.id DESC LIMIT 5"
    ).fetchall()
    conn.close()
    cards = "".join(order_card(o) for o in board) or '<p class="muted">当前没有排队/提货中的订单。</p>'
    prod_opts = "".join(f'<option value="{p["code"]}">{esc(p["code"])} {esc(p["name"])}</option>' for p in products)
    cust_opts = "".join(f'<option value="{c["code"]}">{esc(c["code"])} {esc(c["name"])}</option>' for c in customers)
    veh_opts = "".join(
        f'<option value="{esc(v["plate"])}|{esc(v["axle_type"])}">{esc(v["plate"])}（{esc(v["axle_type"])}）</option>'
        for v in vehicles)
    done_rows = "".join(
        f'<tr><td><a href="/order/{d["id"]}">{esc(d["order_no"])}</a></td>'
        f'<td>{esc(d["plate"])}</td><td class="num">{d["total_net"]:.3f}</td>'
        f'<td class="num">¥{d["amount"]:,.2f}</td></tr>' for d in done)
    return f"""
    <div class="grid grid-2">
      <div>
        <div class="section-title">提货排队屏（车号后四位 · 砂浆型号 · 当前环节）</div>
        <div class="grid grid-3">{cards}</div>
        <div class="section-title">最近出场结算</div>
        <div class="card"><table>
          <tr><th>订单</th><th>车辆</th><th class="num">净重(t)</th><th class="num">金额</th></tr>
          {done_rows or '<tr><td colspan=4 class="muted">暂无</td></tr>'}
        </table></div>
      </div>
      <div>
        {silo_panel()}
        <div class="card" style="margin-top:14px">
          <h3>登记订单（付款确认后入队）</h3>
          <form method="post" action="/act/create_order" class="stack" style="display:flex;flex-direction:column;align-items:stretch">
            <label>客户 <select name="customer_code">{cust_opts}</select></label>
            <label>砂浆型号 <select name="product_code">{prod_opts}</label>
            <label>订购量(吨) <input name="qty" value="20" inputmode="decimal"></label>
            <label>预约车辆 <select name="vehicle">{veh_opts}</select></label>
            <label>预约时段（分钟后起，30 分钟） <input name="in_min" value="10" inputmode="numeric"></label>
            <button class="yellow" type="submit">登记并确认付款</button>
          </form>
        </div>
      </div>
    </div>"""


# ---------------- 过磅/订单详情页

def weigh_stack(tare, gross, net) -> str:
    def f(v):
        return "—" if v is None else f"{v:.3f}"
    return f"""
    <div class="weigh-stack">
      <div class="row"><span class="lab">皮重</span><span class="val">{f(tare)}<span class="unit"> t</span></span></div>
      <div class="row"><span class="lab">毛重</span><span class="val">{f(gross)}<span class="unit"> t</span></span></div>
      <div class="row net"><span class="lab">出场净重</span><span class="val">{f(net)}<span class="unit"> t</span></span></div>
    </div>"""


def order_page(oid: int, role: str) -> str:
    d = svc.order_detail(oid)
    if d is None:
        return '<p>订单不存在。</p>'
    o, rounds = d["o"], d["rounds"]
    conn = svc._conn()
    silos = conn.execute(
        "SELECT s.* FROM silos s WHERE s.product_code=? ORDER BY s.code",
        (o["product_code"],),
    ).fetchall()
    conn.close()

    blocks = []
    # 顶部信息
    blocks.append(f"""
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:start;flex-wrap:wrap;gap:8px">
        <div>
          <h3 style="font-size:18px">{esc(o['order_no'])}
            <span class="badge gray">{esc(ORDER_CN.get(o['status'], o['status']))}</span>
            <span class="pcode" style="font-size:13px">{esc(o['product_code'])} {esc(o['product_name'])}</span>
          </h3>
          <dl class="kv">
            <dt>客户代号</dt><dd>{esc(o['customer_code'])} {esc(o['customer_name'])}</dd>
            <dt>订购量</dt><dd>{o['qty_ordered']:.3f} t · 已签收 {o['qty_loaded']:.3f} t · 余量 {o['qty_ordered']-o['qty_loaded']:.3f} t</dd>
            <dt>价格版本</dt><dd>{esc(o['price_ver'])} · ¥{o['unit_price']:.2f}/t · 预付 ¥{o['amount_paid']:,.2f}</dd>
            <dt>预约时段</dt><dd>{esc(o['slot_start'])} ~ {esc(o['slot_end'])}</dd>
            <dt>预约车辆</dt><dd>{esc(o['expected_plate'] or '不限车牌')} · {esc(o['expected_axle'] or '不限车型')}</dd>
          </dl>
        </div>
        <div style="text-align:right">
          <div class="plate4">{esc(plate_tail4(o.get('expected_plate') or '----'))}</div>
          <div class="muted">{esc(o.get('expected_plate') or '')}</div>
        </div>
      </div>
    </div>""")

    # 操作区按状态/角色
    actions = action_panel(d, silos, role)
    if actions:
        blocks.append(f'<div class="card warn"><h3>操作 · 当前角色：{esc(ROLE_NAMES.get(role, role))}</h3>{actions}</div>')

    # 各轮过磅：竖直对齐三重量
    rcards = []
    for r in rounds:
        rev_rows = ""
        revs = [x for x in d["revisions"] if x["round_id"] == r["id"]]
        if revs:
            rev_rows = "".join(
                f'<div class="audit-note">人工修订 {esc(x["field"])}：'
                f'<span class="oldval">{x["old_value"]:g}</span>'
                f'<span class="newval">{x["new_value"]:g}</span>'
                f' · {esc(x["status"])} · 发起 {esc(x["requested_by"])}'
                f'{(" / 复核 " + esc(x["reviewed_by"])) if x["reviewed_by"] else ""}</div>'
                for x in revs)
        rcards.append(f"""
        <div class="card {'danger' if r['status'] in ('rejected','disputed') else ''}">
          <h3>第 {r['seq']} 轮 · {esc(ROUND_CN.get(r['status'], r['status']))}
            {'<span class="badge ok">已放行</span>' if r['gate_released'] else ''}
            <span class="muted">料仓 {esc(r['silo_code'] or '—')} · 锁定 {r['lock_qty']:.3f}t</span></h3>
          {weigh_stack(r['tare'], r['gross'], r['net'])}
          <div class="muted" style="text-align:right">
            原始净重 {('%.3f' % r['net_raw']) if r['net_raw'] is not None else '—'} t ·
            含水修正 {esc(r['moisture_pct'])}% · 毛重−皮重基线见审计事件
          </div>
          {rev_rows}
        </div>""")
    if rcards:
        blocks.append('<div class="section-title">两次过磅记录（皮重 / 毛重 / 净重 竖直对齐）</div>'
                      + '<div class="grid grid-3">' + "".join(rcards) + "</div>")

    # 异常留痕
    inc_html = []
    for i in d["incidents"]:
        inc_html.append(f"""
        <div class="incident {'resolved' if i['status']=='resolved' else ''}">
          <b>{esc(INCIDENT_CN.get(i['code'], i['code']))}</b>
          <span class="badge {'green' if i['status']=='resolved' else 'red'}">{esc(i['status'])}</span>
          <div>{esc(i['detail'])}</div>
          <div class="audit-note">{esc(i['created_at'])}
            {(' · 关闭：' + esc(i['resolution'])) if i['status']=='resolved' else ''}
            {(' · ' + esc(i['resolved_at'])) if i['resolved_at'] else ''}</div>
          {resolve_form(i, role) if i['status']=='open' else ''}
        </div>""")
    if inc_html:
        blocks.append('<div class="section-title">异常留痕（审计原值保留）</div>'
                      + '<div class="card">' + "".join(inc_html) + "</div>")

    # 结算快照
    if d["settlement"]:
        s = d["settlement"]
        snap = json.loads(s["snapshot"])
        rrows = "".join(
            f'<tr><td>第{x["seq"]}轮</td><td>{x["tare"]:.3f}</td><td>{x["gross"]:.3f}</td>'
            f'<td>{x["moisture_pct"]}%</td><td class="num">{x["net_raw"]:.3f}</td>'
            f'<td class="num">{x["net"]:.3f}</td><td>{esc(x["silo"] or "—")}</td></tr>'
            for x in snap["rounds"])
        refund_html = ""
        if d["refund"]:
            rf = d["refund"]
            refund_html = f"""<p>退款单：{esc(rf['status'])} · {rf['qty']:.3f}t · ¥{rf['amount']:,.2f}
                （发起 {esc(rf['requested_by'])}{(' / 复核 ' + esc(rf['reviewed_by'])) if rf['reviewed_by'] else ''}）</p>"""
        blocks.append(f"""
        <div class="snapshot">
          <h3 style="color:#fff;margin:0 0 8px">结算快照（不可变） · 价格版本 {esc(s['price_ver'])}</h3>
          <table>
            <tr><th>轮次</th><th>皮重</th><th>毛重</th><th>含水</th><th class="num">原始净重</th><th class="num">修正净重</th><th>料仓</th></tr>
            {rrows}
          </table>
          <div style="display:flex;justify-content:space-between;align-items:flex-end;margin-top:10px;flex-wrap:wrap">
            <div>合计出场净重 <span class="big">{s['total_net']:.3f}</span> t</div>
            <div style="text-align:right">
              <div style="font-size:22px">¥{s['amount']:,.2f}</div>
              <div class="muted" style="color:#c9d4e8">预付 ¥{s['paid']:,.2f} ·
                已退 ¥{s['refund_amt']:,.2f}</div>
            </div>
          </div>
          {refund_html}
          <div class="muted" style="color:#c9d4e8">放行：{esc(snap['released_by'])} · {esc(snap['released_at'])}</div>
        </div>""")

    # 图片（本地目录：车辆入场照、出场磅单）
    if d["images"]:
        imgs = "".join(
            f'<figure style="display:inline-block;margin:4px;text-align:center">'
            f'<a href="/images/{esc(x["filename"])}" target="_blank">'
            f'<img class="img-thumb" src="/images/{esc(x["filename"])}" alt="{esc(x["kind"])}"></a>'
            f'<figcaption class="muted">{"车辆入场" if x["kind"]=="vehicle" else "出场磅单"}'
            f'<br>{esc(x["created_at"][-8:])}</figcaption></figure>'
            for x in d["images"])
        blocks.append(f'<div class="card"><h3>车辆与磅单图片（本地目录）</h3>{imgs}</div>')

    # 事件流
    ev_rows = "".join(
        f'<tr><td>{esc(e["created_at"])}</td><td>{esc(e["kind"])}</td>'
        f'<td class="num">{("%.3f" % e["tare"]) if e["tare"] is not None else "—"}</td>'
        f'<td class="num">{("%.3f" % e["gross"]) if e["gross"] is not None else "—"}</td>'
        f'<td class="num">{("%.3f" % e["net"]) if e["net"] is not None else "—"}</td>'
        f'<td>{esc(e["silo_code"] or "—")}</td><td>{esc(e["note"])}</td><td>{esc(e["created_by"])}</td></tr>'
        for e in d["events"])
    blocks.append(f"""
    <details class="card" style="margin-top:6px"><summary><b>重量事件流（追加写，{len(d['events'])} 条）</b></summary>
    <table><tr><th>时间</th><th>事件</th><th>皮重</th><th>毛重</th><th>净重</th><th>料仓</th><th>说明</th><th>操作人</th></tr>
    {ev_rows}</table></details>""")
    return "".join(blocks)


def resolve_form(i: dict, role: str) -> str:
    if role != ROLE_SUPERVISOR:
        return '<div class="muted">需当班主管登录后关闭</div>'
    return f"""
    <form method="post" action="/act/resolve_incident" class="stack">
      <input type="hidden" name="incident_id" value="{i['id']}">
      <input name="resolution" placeholder="处理结论" style="flex:1" required>
      <button class="small yellow">主管关闭异常</button>
    </form>"""


def action_panel(d: dict, silos, role: str) -> str:
    o = d["o"]
    rounds = d["rounds"]
    last = rounds[-1] if rounds else None
    svc_row = svc.active_service_for_order(svc._conn(), o["id"])
    out = []

    def btn(act, label, **fields):
        hs = "".join(f'<input type="hidden" name="{k}" value="{esc(v)}">' for k, v in fields.items())
        return f'<form method="post" action="/act/{act}" class="stack">{hs}<button>{label}</button></form>'

    if o["status"] == "queued":
        if role == ROLE_DISPATCHER and not o["called_at"]:
            out.append(btn("call_order", "调度呼号", order_id=o["id"]))
        if role == ROLE_DISPATCHER:
            opts = "".join(
                f'<option value="{esc(v["plate"])}|{esc(v["axle_type"])}">'
                f'{esc(v["plate"])}（{esc(v["axle_type"])}）</option>'
                for v in svc._conn().execute("SELECT * FROM vehicles ORDER BY plate"))
            out.append(f"""
            <form method="post" action="/act/enter_tare" class="stack">
              <input type="hidden" name="order_id" value="{o['id']}">
              车辆入场首次过磅
              <select name="vehicle">{opts}</select>
              <button class="yellow">记录皮重并锁定订单</button>
            </form>""")

    if o["status"] == "serving" and svc_row and last is not None and not last["gate_released"]:
        silo_opts = "".join(
            f'<option value="{s["code"]}">{esc(s["code"])}（{esc(s["product_code"])}，存 {s["stock"]:.1f}t）</option>'
            for s in silos)
        if last["status"] == "tared" and role == ROLE_OPERATOR:
            out.append(f"""
            <form method="post" action="/act/begin_load" class="stack">
              <input type="hidden" name="round_id" value="{last['id']}">
              料仓 <select name="silo_code">{silo_opts}</select>
              申请装料 <input name="req_qty" value="{max(o['qty_ordered']-o['qty_loaded'],0):.2f}"
                 style="width:90px" inputmode="decimal"> t
              <button class="yellow">按锁定量发料</button>
            </form>""")
        if last["status"] == "loading" and role == ROLE_OPERATOR:
            out.append(f"""
            <form method="post" action="/act/record_gross" class="stack">
              <input type="hidden" name="round_id" value="{last['id']}">
              含水修正 <input name="moisture_pct" value="3" style="width:60px"> %
              <button class="yellow">装料后二次过磅</button>
            </form>""")
        if last["status"] == "loaded" and role == ROLE_CUSTOMER:
            out.append(f"""
            <form method="post" action="/act/sign" class="stack">
              <input type="hidden" name="round_id" value="{last['id']}">
              <button class="yellow">客户确认重量并签收</button>
            </form>
            <form method="post" action="/act/reject" class="stack">
              <input type="hidden" name="round_id" value="{last['id']}">
              <input name="reason" placeholder="拒签原因" style="flex:1" required>
              <button class="red">拒签</button>
            </form>
            <form method="post" action="/act/dispute" class="stack">
              <input type="hidden" name="round_id" value="{last['id']}">
              主张含水率 <input name="claimed_pct" value="5" style="width:60px"> %
              <button class="gray">提出含水争议</button>
            </form>""")
        if last["status"] == "signed" and not last["gate_released"]:
            if role == ROLE_OPERATOR and o["qty_loaded"] + 0.005 < o["qty_ordered"]:
                out.append(btn("topup", "在订单余量内补装（开新一轮）", order_id=o["id"]))
            if role == ROLE_DISPATCHER:
                out.append(btn("release", "客户已确认·核验放行出场并生成结算快照",
                               order_id=o["id"]))

    # 复核台快捷动作
    if role == ROLE_OPERATOR and last and last["status"] in ("loaded",) and not last["gate_released"]:
        out.append(f"""
        <details><summary class="muted">人工改重申请（主管复核）</summary>
        <form method="post" action="/act/request_revision" class="stack">
          <input type="hidden" name="round_id" value="{last['id']}">
          <select name="field"><option value="tare">皮重</option>
          <option value="gross">毛重</option><option value="moisture_pct">含水率</option></select>
          新值 <input name="new_value" style="width:90px" inputmode="decimal" required>
          <input name="reason" placeholder="改重原因" style="flex:1" required>
          <button class="small">发起改重</button>
        </form></details>""")
    return "".join(out)


def review_page(role: str) -> str:
    conn = svc._conn()
    revs = conn.execute(
        "SELECT wr.*, lr.order_id, lr.seq, lr.plate FROM weight_revisions wr"
        " JOIN load_rounds lr ON lr.id=wr.round_id WHERE wr.status='pending' ORDER BY wr.id"
    ).fetchall()
    refunds = conn.execute(
        "SELECT r.*, o.order_no FROM refunds r JOIN orders o ON o.id=r.order_id"
        " WHERE r.status='pending' ORDER BY r.id"
    ).fetchall()
    conn.close()
    rev_cards = []
    for x in revs:
        rev_cards.append(f"""
        <div class="card warn">
          <b>订单 <a href="/order/{x['order_id']}">#{x['order_id']}</a> 第 {x['seq']} 轮 · {esc(x['plate'])}</b><br>
          人工修订 {esc(x['field'])}：
          <span class="oldval">{x['old_value']:g}</span>→<span class="newval">{x['new_value']:g}</span>
          <div class="audit-note">原因：{esc(x['reason'])} · 发起人 {esc(x['requested_by'])} · {esc(x['requested_at'])}</div>
          <form method="post" action="/act/review_revision" class="stack">
            <input type="hidden" name="revision_id" value="{x['id']}">
            <input name="note" placeholder="复核意见" style="flex:1">
            <button name="approve" value="1" class="yellow small">主管批准（重算净重）</button>
            <button name="approve" value="0" class="gray small">驳回</button>
          </form>
          <div class="muted">仅当班主管可批，且不能批准本人发起的申请。</div>
        </div>""")
    rf_cards = []
    for x in refunds:
        rf_cards.append(f"""
        <div class="card warn">
          <b>订单 <a href="/order/{x['order_id']}">{esc(x['order_no'])}</a></b>
          退款 {x['qty']:.3f}t · <b>¥{x['amount']:,.2f}</b>
          <div class="audit-note">事由：{esc(x['reason'])} · 发起人 {esc(x['requested_by'])}</div>
          <form method="post" action="/act/review_refund" class="stack">
            <input type="hidden" name="refund_id" value="{x['id']}">
            <input name="note" placeholder="复核意见" style="flex:1">
            <button name="approve" value="1" class="yellow small">财务主管批准付款</button>
            <button name="approve" value="0" class="gray small">驳回</button>
          </form>
        </div>""")

    # 已结算未退款的订单，财务可发起退款
    conn = svc._conn()
    sett = conn.execute(
        "SELECT s.*, o.order_no, o.unit_price FROM settlements s JOIN orders o ON o.id=s.order_id"
        " WHERE NOT EXISTS (SELECT 1 FROM refunds r WHERE r.order_id=s.order_id)"
        " ORDER BY s.id DESC LIMIT 10"
    ).fetchall()
    conn.close()
    rf_start = "".join(f"""
      <tr><td><a href="/order/{s['order_id']}">{esc(s['order_no'])}</a></td>
      <td class="num">{s['total_net']:.3f}</td><td class="num">¥{s['amount']:,.2f}</td>
      <td><form method="post" action="/act/request_refund" class="stack">
        <input type="hidden" name="order_id" value="{s['order_id']}">
        <input name="reason" placeholder="退款原因" required>
        <button class="small">财务发起退款（幂等）</button></form></td></tr>""" for s in sett)
    return f"""
    <div class="grid grid-2">
      <div>
        <div class="section-title">人工改重复核（操作员发起 → 主管复核）</div>
        {''.join(rev_cards) or '<p class="muted">无待复核改重。</p>'}
        <div class="section-title">退款复核（财务发起 → 财务主管付款）</div>
        {''.join(rf_cards) or '<p class="muted">无待复核退款。</p>'}
      </div>
      <div class="card">
        <h3>已结算订单退款入口</h3>
        <table><tr><th>订单</th><th class="num">净重</th><th class="num">金额</th><th>退款</th></tr>
        {rf_start or '<tr><td colspan=4 class="muted">暂无</td></tr>'}</table>
        <p class="muted">退款差额按 订购量−出场净重 自动计算；同一订单重复提交返回原单。</p>
      </div>
    </div>"""


def incidents_page(role: str) -> str:
    conn = svc._conn()
    rows = conn.execute(
        "SELECT i.*, o.order_no FROM incidents i LEFT JOIN orders o ON o.id=i.order_id"
        " ORDER BY i.status='resolved', i.id DESC"
    ).fetchall()
    conn.close()
    body = []
    for i in rows:
        body.append(f"""
        <div class="incident {'resolved' if i['status']=='resolved' else ''}">
          <b>{esc(INCIDENT_CN.get(i['code'], i['code']))}</b>
          <code>{esc(i['code'])}</code>
          <span class="badge {'green' if i['status']=='resolved' else 'red'}">{esc(i['status'])}</span>
          {'<a class="small" href="/order/%d">订单 #%d</a>' % (i['order_id'], i['order_id']) if i['order_id'] else ''}
          <div>{esc(i['detail'])}</div>
          <div class="audit-note">{esc(i['created_at'])} · 车辆 {esc(i['plate'] or '—')}
            {(' · 关闭于 ' + esc(i['resolved_at']) + '：' + esc(i['resolution'])) if i['status']=='resolved' else ''}</div>
        </div>""")
    return f'<div class="card">{"".join(body) or "<p class=muted>暂无异常</p>"}</div>'


def control_page(role: str) -> str:
    outage = adapters.scale.outage
    btn = '<button name="outage" value="1" class="red">切换为：地磅中断</button>' if not outage \
        else '<button name="outage" value="0" class="yellow">恢复地磅通讯</button>'
    return f"""
    <div class="card">
      <h3>模拟适配器控制台</h3>
      <p>当前地磅：<b>{'中断' if outage else '正常'}</b>（中断后"装料后二次过磅"会留痕地磅中断事件）</p>
      <form method="post" action="/act/toggle_scale" class="stack">{btn}</form>
      <div class="hr"></div>
      <form method="post" action="/act/reseed" class="stack"
            onsubmit="return confirm('将清空并重建全部虚构样例数据，确定？')">
        <button class="gray">重置全部样例数据（超量 / 断磅 / 拒签 / 争议 / 并发抢料 / 过号）</button>
      </form>
      <form method="post" action="/act/sweep" class="stack">
        <button class="small gray">立即执行过号扫描（宽限 {GRACE_SECONDS}s）</button>
      </form>
    </div>"""


# ================================================================ HTTP

from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse


class WebHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _cookies(self):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        try:
            return c["role"].value
        except Exception:
            return ROLE_DISPATCHER

    def _send(self, content, status=200, ctype="text/html; charset=utf-8",
              extra_headers=None):
        data = content if isinstance(content, bytes) else content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, path, msg="", err=""):
        target = getattr(self, "_redirect_target", None) or path
        self._redirect_target = None
        q = urllib.parse.urlencode({"msg": msg, "err": err})
        loc = target + (("?" + q) if (msg or err) else "")
        self._send("", HTTPStatus.FOUND, extra_headers=[("Location", loc)])

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        role = self._cookies()
        q = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        try:
            if path == "/":
                self._send(shell("排队屏", board_page(role), role,
                                 q.get("msg", ""), q.get("err", "")))
            elif path.startswith("/order/"):
                oid = int(path.rsplit("/", 1)[-1])
                self._send(shell(f"订单 {oid}", order_page(oid, role), role,
                                 q.get("msg", ""), q.get("err", "")))
            elif path == "/review":
                self._send(shell("复核台", review_page(role), role,
                                 q.get("msg", ""), q.get("err", "")))
            elif path == "/incidents":
                self._send(shell("异常留痕", incidents_page(role), role))
            elif path == "/control":
                self._send(shell("模拟控制台", control_page(role), role))
            elif path.startswith("/static/"):
                self._static(path.split("/static/", 1)[1])
            elif path.startswith("/images/"):
                self._image(path.split("/images/", 1)[1])
            else:
                self._send(shell("未找到", "<p>页面不存在。</p>", role), 404)
        except Exception as exc:
            self._send(shell("错误", f"<div class='flash err'>{esc(repr(exc))}</div>", role), 500)

    def _static(self, name):
        name = urllib.parse.unquote(name)
        p = (STATIC / name).resolve()
        if not str(p).startswith(str(STATIC.resolve())) or not p.is_file():
            self._send("not found", 404, "text/plain")
            return
        ctype = "text/css" if p.suffix == ".css" else "application/octet-stream"
        self._send(p.read_text(), ctype=ctype)

    def _image(self, name):
        from .config import IMG_DIR
        name = urllib.parse.unquote(name)
        p = (IMG_DIR / name).resolve()
        if not str(p).startswith(str(IMG_DIR.resolve())) or not p.is_file():
            self._send("not found", 404, "text/plain")
            return
        self._send(p.read_bytes(), ctype="image/png")

    def do_POST(self):
        parsed = urlparse(self.path)
        role = self._cookies()
        length = int(self.headers.get("Content-Length", 0))
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
        actor_defaults = {
            ROLE_DISPATCHER: "dispatcher-张敏", ROLE_OPERATOR: "operator-周强",
            ROLE_SUPERVISOR: "supervisor-赵强", ROLE_FINANCE: "finance-李娜",
            ROLE_FINANCE_MGR: "finance_mgr-孙总", ROLE_CUSTOMER: "customer-王经理",
        }
        actor = actor_defaults.get(role, role)
        try:
            back = f'/order/{form.get("order_id")}' if form.get("order_id") else "/"
            if parsed.path == "/set-role":
                self._send("", HTTPStatus.FOUND, extra_headers=[
                    ("Location", form.get("back") or "/"),
                    ("Set-Cookie", f'role={form.get("role", ROLE_DISPATCHER)}; Path=/')])
                return
            self._route_action(parsed.path, form, role, actor)
            self._redirect(back, msg="操作成功")
        except svc.DomainError as e:
            self._redirect(back, err=f"[{e.code}] {e}")
        except Exception as e:
            self._redirect(back, err=repr(e))

    def _route_action(self, path, f, role, actor):
        from datetime import datetime, timedelta
        if path == "/act/create_order":
            plate, axle = f["vehicle"].split("|", 1)
            t0 = datetime.now() + timedelta(minutes=int(float(f["in_min"])))
            t1 = t0 + timedelta(minutes=30)
            oid = svc.create_order(
                customer_code=f["customer_code"], product_code=f["product_code"],
                qty=float(f["qty"]), slot_start=t0.strftime("%Y-%m-%d %H:%M"),
                slot_end=t1.strftime("%Y-%m-%d %H:%M"),
                expected_plate=plate, expected_axle=axle, by=actor)
            svc.confirm_payment(oid, by=actor)
            self._redirect_target = f"/order/{oid}"
        elif path == "/act/call_order":
            svc.call_order(int(f["order_id"]), by=actor)
        elif path == "/act/enter_tare":
            plate, axle = f["vehicle"].split("|", 1)
            svc.enter_and_tare(order_id=int(f["order_id"]), plate=plate,
                               axle_type=axle, by=actor)
        elif path == "/act/begin_load":
            svc.begin_load(int(f["round_id"]), f["silo_code"], float(f["req_qty"]),
                           by=actor)
        elif path == "/act/record_gross":
            svc.record_gross(int(f["round_id"]), by=actor,
                             moisture_pct=float(f.get("moisture_pct", 3)))
        elif path == "/act/sign":
            svc.customer_sign(int(f["round_id"]), actor=actor)
        elif path == "/act/reject":
            svc.customer_reject(int(f["round_id"]), f["reason"], actor=actor)
        elif path == "/act/dispute":
            svc.dispute_moisture(int(f["round_id"]), float(f["claimed_pct"]), by=actor)
        elif path == "/act/topup":
            rid = svc.request_topup(int(f["order_id"]), by=actor)
            self._redirect_target = f"/order/{f['order_id']}"
            return
        elif path == "/act/release":
            svc.release_gate(int(f["order_id"]), actor=actor)
        elif path == "/act/request_revision":
            svc.request_revision(int(f["round_id"]), f["field"],
                                 float(f["new_value"]), f["reason"], actor, role)
        elif path == "/act/review_revision":
            svc.review_revision(int(f["revision_id"]), f.get("approve") == "1",
                                actor, role, f.get("note", ""))
            self._redirect_target = "/review"
        elif path == "/act/resolve_incident":
            svc.resolve_incident(int(f["incident_id"]), f["resolution"], actor, role)
        elif path == "/act/request_refund":
            svc.request_refund(int(f["order_id"]), f["reason"], actor, role)
        elif path == "/act/review_refund":
            svc.review_refund(int(f["refund_id"]), f.get("approve") == "1",
                              actor, role, f.get("note", ""))
            self._redirect_target = "/review"
        elif path == "/act/toggle_scale":
            adapters.scale.outage = f.get("outage") == "1"
            self._redirect_target = "/control"
        elif path == "/act/reseed":
            seed()
            self._redirect_target = "/"
        elif path == "/act/sweep":
            svc.sweep_no_shows(GRACE_SECONDS)
            self._redirect_target = "/"
        else:
            raise svc.DomainError("no_route", f"未知操作 {path}")


# ================================================================ 后台过号任务

def sweep_loop():
    while True:
        time.sleep(SWEEP_INTERVAL)
        try:
            svc.sweep_no_shows(GRACE_SECONDS)
        except Exception:
            pass


def run(host=HOST, port=PORT, do_seed=True):
    from http.server import ThreadingHTTPServer
    if do_seed:
        seed()
    t = threading.Thread(target=sweep_loop, daemon=True)
    t.start()
    httpd = ThreadingHTTPServer((host, port), WebHandler)
    print(f"散装砂浆提货过磅系统已启动： http://{host}:{port}/ （Ctrl+C 停止）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
