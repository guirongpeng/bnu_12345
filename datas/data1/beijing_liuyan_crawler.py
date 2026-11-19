# -*- coding: utf-8 -*-
"""人民网领导留言板-北京市公开留言采集（跳过东城区，采集3类留言）"""
import argparse, hashlib, json, os, sys, time, urllib.parse, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta
from http.cookiejar import CookieJar

APP_CODE = "PC42ce3bfa4980a9"
SECRET = hashlib.md5(APP_CODE.encode("utf-8")).hexdigest()[:16]
BASE = "https://liuyan.people.com.cn"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36 Edg/156.0.0.0"
TYPE_NAMES = {5: "建言", 2: "投诉/求助", 1: "咨询"}
TZ = timezone(timedelta(hours=8))

def jdumps(o): return json.dumps(o, ensure_ascii=False, separators=(",", ":"))
def sign(path, params):
    body = jdumps(params)
    return hashlib.md5((path + body + SECRET).encode("utf-8")).hexdigest(), body

class Client:
    def __init__(self, delay=0.6, timeout=40, retries=6):
        self.delay, self.timeout, self.retries = delay, timeout, retries
        self.last = 0.0
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    def _wait(self):
        d = self.delay - (time.time() - self.last)
        if d > 0: time.sleep(d)
        self.last = time.time()
    def _headers(self, referer):
        return {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Content-Type": "application/json;charset=UTF-8", "Origin": BASE,
                "Referer": referer or (BASE + "/forum/list?province=4"),
                "sec-ch-ua": '"Chromium";v="156", "Not(A:Brand";v="24", "Microsoft Edge";v="156"',
                "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"'}
    def _request(self, req, path, params):
        last_err = None
        for attempt in range(self.retries):
            self._wait()
            try:
                with self.opener.open(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code in (403,408,429,500,502,503,504):
                    w = min(30, 2 ** attempt)
                    print(f"  [重试] HTTP {e.code}，{w}s ({attempt+1}/{self.retries})", flush=True)
                    time.sleep(w); continue
                try: print("  [HTTP错误]", e.read().decode("utf-8","replace")[:500], flush=True)
                except Exception: pass
                raise
            except Exception as e:
                last_err = e; w = min(30, 2 ** attempt)
                print(f"  [重试] {type(e).__name__}: {e}，{w}s ({attempt+1}/{self.retries})", flush=True)
                time.sleep(w)
        raise RuntimeError(f"请求失败: {path} {params!r}: {last_err}")
    def post(self, path, params, referer=None):
        sig, body = sign(path, params)
        payload = jdumps({"appCode": APP_CODE, "token": "", "signature": sig, "param": body}).encode("utf-8")
        req = urllib.request.Request(BASE + path, data=payload, headers=self._headers(referer), method="POST")
        return self._request(req, path, params)
    def get(self, path, params, referer=None):
        p = dict(params); p["_t"] = int(time.time()*1000)
        sig, body = sign(path, p)
        q = urllib.parse.urlencode({"appCode": APP_CODE, "token": "", "signature": sig, "param": body})
        req = urllib.request.Request(BASE + path + "?" + q, headers=self._headers(referer), method="GET")
        return self._request(req, path, p)

def ts2str(ts):
    if not ts: return ""
    try: return datetime.fromtimestamp(int(ts), TZ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception: return ""
def read_json(path, default=None):
    if not os.path.exists(path): return default
    try:
        with open(path, "r", encoding="utf-8") as f: return json.load(f)
    except Exception: return default
def write_json(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def fetch_forum_tree(client, outdir):
    path = os.path.join(outdir, "forum_tree.json")
    cached = read_json(path)
    if cached:
        print("读取论坛树缓存:", path, flush=True); return cached
    print("获取北京市论坛/领导层级 ...", flush=True)
    r = client.get("/v1/forum/getChildForums", {"fid": 4}, referer=BASE+"/forum/list?province=4")
    if r.get("code") != 0: raise RuntimeError(f"获取北京市下级论坛失败: {r}")
    districts = []
    for item in r["data"]:
        d = {"fid": item["fid"], "name": item["name"], "hasChild": item.get("hasChild",0),
             "threadsNum": item.get("threadsNum"), "tnumYear": item.get("tnumYear"), "leaders": []}
        if item.get("hasChild") and item.get("fid") != 638:
            rr = client.get("/v1/forum/getChildForums", {"fid": item["fid"]},
                            referer=BASE+f"/forum/list?province=4&fid={item['fid']}")
            if rr.get("code") != 0: raise RuntimeError(f"获取 {item['name']} 下级领导失败: {rr}")
            d["leaders"] = [{"fid": x["fid"], "name": x["name"], "threadsNum": x.get("threadsNum"),
                             "tnumYear": x.get("tnumYear")} for x in rr.get("data", [])]
        districts.append(d)
    tree = {"fetchedAt": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "province": "北京市", "provinceFid": 4, "children": districts}
    write_json(path, tree); print("论坛树已保存:", path, flush=True); return tree

def crawl_list_combo(client, outdir, fid, forum_name, district, level, type_id, max_pages=None):
    listdir = os.path.join(outdir, "lists"); os.makedirs(listdir, exist_ok=True)
    cachefile = os.path.join(listdir, f"{fid}_{type_id}.json")
    cached = read_json(cachefile)
    if cached and cached.get("complete"):
        print(f"  已有缓存: {forum_name} / {TYPE_NAMES[type_id]}  {len(cached.get('items',[]))} 条", flush=True)
        return cached
    records, seen, last_item, page, hit_cap = [], set(), "", 0, False
    while True:
        page += 1
        params = {"fid": fid, "showUnAnswer": 1, "typeId": type_id, "lastItem": last_item,
                  "position": 0, "rows": 100000, "orderType": 2}
        r = client.post("/v1/threads/list/df", params, referer=BASE+f"/threads/list?fid={fid}")
        if r.get("result", {}).get("code") != "0":
            raise RuntimeError(f"留言列表接口失败 fid={fid} type={type_id}: {r.get('result')}")
        rd = r.get("resultData") or {}; data = rd.get("data") or []; new = 0
        for x in data:
            tid = x.get("tid")
            if tid and tid not in seen:
                seen.add(tid); rec = dict(x)
                rec.update({"_district": district, "_level": level,
                            "_forumName": forum_name, "_typeName": TYPE_NAMES[type_id]})
                records.append(rec); new += 1
        print(f"    {forum_name} / {TYPE_NAMES[type_id]} 第{page}页 +{new}，累计 {len(records)}", flush=True)
        if max_pages and page >= max_pages: hit_cap = True; break
        if not data: break
        new_last = rd.get("lastItem") or ""
        if not new_last or new_last == last_item: break
        last_item = new_last
        if len(data) < 100000: break
    result = {"fid": fid, "forumName": forum_name, "district": district, "level": level,
              "typeId": type_id, "typeName": TYPE_NAMES[type_id], "orderType": 2,
              "showUnAnswer": 1, "complete": not hit_cap,
              "fetchedAt": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"), "items": records}
    write_json(cachefile, result); return result

def crawl_detail(client, outdir, tid):
    ddir = os.path.join(outdir, "details"); os.makedirs(ddir, exist_ok=True)
    path = os.path.join(ddir, f"{tid}.json")
    cached = read_json(path)
    if cached is not None: return cached
    r = client.post("/v1/threads/content", {"tid": tid, "fromTrash": 0},
                    referer=BASE+f"/threads/content?tid={tid}")
    if r.get("result", {}).get("code") != "0":
        obj = {"tid": tid, "ok": False, "error": r.get("result")}
    else:
        obj = {"tid": tid, "ok": True, "resultData": r.get("resultData")}
    write_json(path, obj); return obj

def build_rows(tree, list_results, details):
    rows, detail_map = [], {}
    for d in details:
        if d and d.get("ok"): detail_map[d["tid"]] = d.get("resultData") or {}
    for combo in list_results:
        for x in combo.get("items", []):
            tid = x.get("tid"); det = detail_map.get(tid) or {}
            contents = det.get("contentList") or []
            original = contents[0] if contents else x
            answers = [c for c in contents[1:] if c.get("dataType") == 4 or c.get("answerState") is not None]
            answer_texts, orgs, answer_times = [], [], []
            for a in answers:
                txt = (a.get("content") or "").strip()
                if txt: answer_texts.append(txt)
                if a.get("organization"): orgs.append(str(a.get("organization")))
                if a.get("dateline"): answer_times.append(int(a.get("dateline")))
            if not answers:
                for k in ("answerContent", "answer", "replyContent"):
                    if x.get(k): answer_texts.append(str(x.get(k)))
            rows.append({
                "留言ID": tid,
                "留言链接": f"https://liuyan.people.com.cn/threads/content?tid={tid}",
                "层级": x.get("_level", combo.get("level", "")),
                "区划": x.get("_district", combo.get("district", "")),
                "领导": x.get("forumName") or x.get("_forumName") or combo.get("forumName", ""),
                "留言类型": x.get("typeName") or combo.get("typeName", ""),
                "领域": x.get("domainName", ""), "标题": x.get("subject", ""),
                "留言内容": (original.get("content") or x.get("content") or "").strip(),
                "留言人": original.get("nickName") or x.get("nickName", ""),
                "来源": original.get("from") or x.get("from", ""),
                "事发地": original.get("threadPlace") or x.get("threadPlace", ""),
                "留言时间": ts2str(original.get("createDateline") or x.get("createDateline") or x.get("dateline")),
                "办理状态": x.get("stateInfo", ""), "状态说明": x.get("traceStateDesc", ""),
                "最新回复时间": ts2str(max(answer_times)) if answer_times else ts2str(x.get("lastAnswerTime")),
                "回复单位": "；".join(dict.fromkeys(orgs)),
                "回复内容": "\n\n".join(answer_texts),
                "浏览量": (x.get("threadsCount") or {}).get("viewNum", ""),
                "点赞数": (x.get("threadsCount") or {}).get("likesNum", ""),
                "附议数": (x.get("threadsCount") or {}).get("agreeNum", ""),
                "评论数": (x.get("threadsCount") or {}).get("commentNum", ""),
                "是否公开": "是" if x.get("publicState") == 1 else "否",
                "采集时间": combo.get("fetchedAt", ""),
            })
    return rows

def export_excel(tree, list_results, rows, outfile):
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
    from openpyxl.utils import get_column_letter
    from collections import Counter
    wb = Workbook(); ws = wb.active; ws.title = "留言明细"
    headers = ["序号","留言ID","留言链接","层级","区划","领导","留言类型","领域","标题","留言内容",
               "留言人","来源","事发地","留言时间","办理状态","状态说明","最新回复时间","回复单位",
               "回复内容","浏览量","点赞数","附议数","评论数","是否公开","采集时间"]
    ws.append(headers)
    import re
    def clean_cell(v):
        if v is None: return ""
        s = str(v)
        s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)
        return s[:32000]
    for i, r in enumerate(rows, 1):
        ws.append([i] + [clean_cell(r.get(h, "")) for h in headers[1:]])
    ws.freeze_panes = "A2"; ws.auto_filter.ref = ws.dimensions
    hf = PatternFill("solid", fgColor="D9EAF7"); thin = Side(style="thin", color="D0D0D0")
    bd = Border(left=thin, right=thin, top=thin, bottom=thin)
    for cell in ws[1]:
        cell.font = Font(bold=True); cell.fill = hf
        cell.alignment = Alignment(horizontal="center", vertical="center"); cell.border = bd
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True); cell.border = bd
    widths = [6,12,42,8,16,28,12,10,30,60,14,18,26,20,10,16,20,20,60,10,10,10,10,10,20]
    for i, w in enumerate(widths, 1): ws.column_dimensions[get_column_letter(i)].width = w
    ws.row_dimensions[1].height = 24
    for idx in range(2, ws.max_row+1): ws.row_dimensions[idx].height = 55
    ws2 = wb.create_sheet("统计"); ws2.append(["统计口径","项目","数量"])
    for cell in ws2[1]: cell.font = Font(bold=True); cell.fill = hf
    cd = Counter(r["区划"] for r in rows); ct = Counter(r["留言类型"] for r in rows)
    cl = Counter(r["层级"] for r in rows); cdt = Counter((r["区划"], r["留言类型"]) for r in rows)
    for k,v in sorted(cl.items()): ws2.append(["层级",k,v])
    for k,v in sorted(ct.items()): ws2.append(["留言类型",k,v])
    for k,v in sorted(cd.items()): ws2.append(["区划合计",k,v])
    for (d,t),v in sorted(cdt.items()): ws2.append([d,t,v])
    ws2.append(["总记录","去重后留言数",len(set(r["留言ID"] for r in rows))])
    ws2.column_dimensions["A"].width=24; ws2.column_dimensions["B"].width=28
    ws2.column_dimensions["C"].width=12; ws2.freeze_panes="A2"
    ws3 = wb.create_sheet("采集说明")
    notes = [("数据来源","人民网领导留言板公开页面及公开接口"),
             ("采集范围","北京市市委书记、市长，以及除东城区外的各区区委书记、区长（含北京经济技术开发区）"),
             ("留言类型","建言、投诉/求助、咨询（三种均采集）"),("东城区","按需求跳过，未采集"),
             ("时间范围","公开列表当前可见范围；接口 timeSpan=2（页面显示约近两年）"),
             ("排序","按时间倒序；showUnAnswer=1，包含尚未回复的公开留言"),
             ("采集时间",datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")),
             ("字段说明","回复内容来自留言详情页公开内容；浏览量/点赞数为采集时点值"),
             ("注意","公开页面会动态变化；本表仅用于学习研究，请遵守网站规则并注明来源")]
    for k,v in notes: ws3.append([k,v])
    for cell in ws3["A"]: cell.font=Font(bold=True)
    ws3.column_dimensions["A"].width=18; ws3.column_dimensions["B"].width=90
    for row in ws3.iter_rows():
        for cell in row: cell.alignment=Alignment(vertical="top", wrap_text=True)
    os.makedirs(os.path.dirname(outfile) or ".", exist_ok=True); wb.save(outfile)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default="raw")
    ap.add_argument("--output", default="北京市领导留言板_各区书记区长_建言投诉求助咨询.xlsx")
    ap.add_argument("--delay", type=float, default=0.6)
    ap.add_argument("--max-pages", type=int, default=0)
    ap.add_argument("--skip-details", action="store_true")
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    client = Client(delay=args.delay)
    tree = fetch_forum_tree(client, args.outdir)
    combos = []
    for item in tree["children"]:
        if item["fid"] in (539, 540):
            combos.append((item["fid"], item["name"], "北京市", "市级"))
    for d in tree["children"]:
        if d["fid"] in (539, 540): continue
        for l in d.get("leaders", []):
            combos.append((l["fid"], l["name"], d["name"], "区级"))
    print(f"待采集领导 {len(combos)} 个，留言类型 3 类", flush=True)
    list_results = []
    for idx, (fid, name, district, level) in enumerate(combos, 1):
        print(f"[{idx}/{len(combos)}] {name} ({district}, fid={fid})", flush=True)
        for type_id in (5, 2, 1):
            list_results.append(crawl_list_combo(client, args.outdir, fid, name, district,
                                                 level, type_id, max_pages=(args.max_pages or None)))
    all_items, seen = [], set()
    for lr in list_results:
        for x in lr.get("items", []):
            tid = x.get("tid")
            if tid and tid not in seen: seen.add(tid); all_items.append(x)
    print(f"列表采集完成：共 {len(all_items)} 条去重留言", flush=True)
    details = []
    if not args.skip_details:
        ddir = os.path.join(args.outdir, "details"); have = 0
        if os.path.isdir(ddir): have = len([n for n in os.listdir(ddir) if n.endswith(".json")])
        print(f"开始采集留言详情（已有缓存 {have} 条）...", flush=True)
        for i, x in enumerate(all_items, 1):
            tid = x.get("tid")
            try: details.append(crawl_detail(client, args.outdir, tid))
            except Exception as e:
                print(f"  [详情失败] tid={tid}: {e}", flush=True)
                details.append({"tid": tid, "ok": False, "error": str(e)})
            if i % 20 == 0 or i == len(all_items):
                ok = sum(1 for d in details if d.get("ok"))
                print(f"  详情进度 {i}/{len(all_items)}，成功 {ok}", flush=True)
    rows = build_rows(tree, list_results, details)
    rows.sort(key=lambda r: r.get("留言时间") or "", reverse=True)
    export_excel(tree, list_results, rows, args.output)
    write_json(os.path.join(args.outdir, "rows.json"), rows)
    print(f"完成：{len(rows)} 条留言，已导出 {args.output}", flush=True)

if __name__ == "__main__":
    main()



