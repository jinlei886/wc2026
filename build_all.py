#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_all.py — 一次生成全部 48 队的世界杯花名册数据（在你本机 / GitHub Actions 运行）

满足硬要求：
  · 最终 26 人名单 —— 来自 Wikipedia「2026 FIFA World Cup squads」（各队 5 月底官宣，权威）
  · 完整赛季数据   —— FBref（经 soccerdata），按联赛抓 player season stats，再按 姓名+生年 拼回
  · 最新德转身价   —— Transfermarkt 国家队页（经 ScraperFC），同时取 号码/身高/惯用脚
  · 中文名         —— Wikidata + data/zh_overrides.json（手工兜底）

产出：
  data/squads/{CODE}.json   每队（http/fetch 用）
  data/squads.js            合并版（本地 file:// 双击打开网页用：window.REAL_SQUADS）
  data/manifest.json        校验汇总（人数、门将数、未匹配项）
  unmatched.log             名字没对上的项，人工补 zh_overrides / 别名表

依赖：见 requirements.txt  →  pip install -r requirements.txt

合规：德转 / FBref 爬取违反各自 ToS，仅供个人自用、勿公开分发。soccerdata 默认本地缓存，
请勿高频请求。
"""
from __future__ import annotations
import json, re, sys, time, unicodedata
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtp
try:
    from rapidfuzz import fuzz
except ImportError:
    fuzz = None

SQUADS_PAGE = "https://en.wikipedia.org/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
UA = {"User-Agent": "wc-roster/1.0 (personal use; contact you@example.com)"}

def _get(url, params=None, headers=None, timeout=30, tries=4):
    """带退避重试的 GET（网络抖动/SSL 被掐自动重试）。"""
    last=None
    for i in range(tries):
        try:
            r=requests.get(url, params=params, headers=headers or UA, timeout=timeout)
            r.raise_for_status(); return r
        except Exception as e:
            last=e
            if i<tries-1: time.sleep(1.5*(i+1))
    raise last
SEASON = "2526"            # FBref 赛季写法：2025/26
OUT = Path("data"); SQ = OUT / "squads"
OVERRIDES = OUT / "zh_overrides.json"

# 队名(维基写法 / 别名) -> 你网页里的三字码。对不上的队会进 unmatched.log，按需补别名即可。
NAME2CODE = {
 "argentina":"ARG","france":"FRA","brazil":"BRA","england":"ENG","spain":"ESP","portugal":"POR",
 "germany":"GER","netherlands":"NED","belgium":"BEL","italy":"ITA","croatia":"CRO","uruguay":"URU",
 "united states":"USA","usa":"USA","mexico":"MEX","canada":"CAN","japan":"JPN",
 "south korea":"KOR","korea republic":"KOR","australia":"AUS","iran":"IRN","ir iran":"IRN",
 "saudi arabia":"KSA","qatar":"QAT","senegal":"SEN","morocco":"MAR","nigeria":"NGA","ghana":"GHA",
 "ivory coast":"CIV","côte d'ivoire":"CIV","cote d'ivoire":"CIV","cameroon":"CMR","egypt":"EGY",
 "algeria":"ALG","tunisia":"TUN","ecuador":"ECU","colombia":"COL","peru":"PER","chile":"CHI",
 "paraguay":"PAR","costa rica":"CRC","panama":"PAN","switzerland":"SUI","denmark":"DEN","poland":"POL",
 "serbia":"SRB","austria":"AUT","scotland":"SCO","sweden":"SWE","norway":"NOR","new zealand":"NZL",
 "uzbekistan":"UZB","jordan":"JOR","czech republic":"CZE","czechia":"CZE",
 # 实际参赛、原表缺的队
 "south africa":"RSA","bosnia and herzegovina":"BIH","bosnia":"BIH","haiti":"HAI",
 "turkey":"TUR","türkiye":"TUR","turkiye":"TUR","curaçao":"CUW","curacao":"CUW",
 "cape verde":"CPV","cabo verde":"CPV","iraq":"IRQ",
 "dr congo":"COD","democratic republic of the congo":"COD","congo dr":"COD",
}

# 名单覆盖到的联赛（FBref 写法）。可按当届实际增减。
LEAGUES = ["ENG-Premier League","ESP-La Liga","ITA-Serie A","GER-Bundesliga","FRA-Ligue 1",
           "POR-Primeira Liga","NED-Eredivisie"]

def norm(s:str)->str:
    s=unicodedata.normalize("NFKD", s or "")
    s="".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z ]","",s.lower()).strip()

_CC="?"
def _simp(s):
    """繁体→简体（opencc）。没装 opencc 则原样返回。"""
    global _CC
    if not s: return s or ""
    if _CC=="?":
        _CC=None
        for mk in (lambda:__import__("opencc").OpenCC("t2s"),
                   lambda:__import__("opencc").OpenCC("t2s.json")):
            try: _CC=mk(); break
            except Exception: pass
        if _CC is None: print("  ℹ 未装 opencc，繁体名不会自动转简体（pip install opencc）", file=sys.stderr)
    try: return _CC.convert(s) if _CC else s
    except Exception: return s

def match(name, year, rows):
    t=norm(name); best,sc=None,0
    for r in rows:
        s=fuzz.token_sort_ratio(t,norm(r["name"])) if fuzz else (100 if t==norm(r["name"]) else 0)
        if r.get("year") and year and r["year"]==year: s+=8
        if s>sc: best,sc=r,s
    return best if sc>=85 else None

# --------------------------------------------------------------------------- #
# 1) 名单：Wikipedia 最终名单页（解析 wikitext 模板，最稳）
#    页面格式：==Group X== 下 ===Country=== 下若干 {{nat fs g player|...}} 行
# --------------------------------------------------------------------------- #
def _f(line, *keys):
    for k in keys:
        m=re.search(r"\|\s*"+k+r"\s*=\s*([^|}]*)", line, re.I)
        if m and m.group(1).strip(): return m.group(1).strip()
    return ""
def _delink(s):
    s=re.sub(r"\[\[(?:[^|\]]*\|)?", "", s).replace("]]","")
    s=re.sub(r"\s*\([^)]*(?:footballer|football|soccer|born)[^)]*\)", "", s, flags=re.I)  # 去消歧义括注
    return re.sub(r"['\"{}]", "", s).strip()
def parse_player_line(line):
    if "|name" not in line.lower(): return None
    name=_delink(_f(line,"name"))
    if not name: return None
    num=re.sub(r"\D","",_f(line,"no","number","shirt")) or None
    pos=re.sub(r"[^A-Za-z]","",_f(line,"pos")).upper()[:2]
    caps=re.sub(r"\D","",_f(line,"caps")) or "0"
    goals=re.sub(r"\D","",_f(line,"goals")) or "0"
    club=_delink(_f(line,"club"))
    cap=("captain" in line.lower()) or ("(c)" in line.lower())
    born=year=None
    bd=re.search(r"birth[ _]date[^}]*?}}", line, re.I)
    if bd:
        nums=[int(x) for x in re.findall(r"\d+", bd.group(0))]
        # age2 模板格式：|基准日(2026|6|11)|出生年|月|日 → 出生年取最后一个年份，其后两个数为月/日
        yi=[i for i,n in enumerate(nums) if n>1900]
        if yi:
            i=yi[-1]; yr=nums[i]
            mo=nums[i+1] if i+1<len(nums) and 1<=nums[i+1]<=12 else 1
            da=nums[i+2] if i+2<len(nums) and 1<=nums[i+2]<=31 else 1
            born=f"{yr:04d}-{mo:02d}-{da:02d}"; year=yr
    return {"num":int(num) if num else None,"en":name,"pos":pos,"born":born,"year":year,
            "caps":int(caps),"intl":int(goals),"club":club,"cap":cap}

def fetch_rosters() -> dict:
    WTC=OUT/"wikitext_cache.txt"
    try:
        wt = _get(SQUADS_PAGE, params={
            "action":"parse","page":"2026_FIFA_World_Cup_squads","prop":"wikitext","format":"json",
        }, timeout=40).json()["parse"]["wikitext"]["*"]
        OUT.mkdir(parents=True, exist_ok=True); WTC.write_text(wt)
    except Exception as e:
        if WTC.exists():
            print(f"  ⚠ 维基拉取失败（{type(e).__name__}），改用上次缓存的名单页")
            wt=WTC.read_text()
        else:
            raise
    code_n={norm(k):v for k,v in NAME2CODE.items()}     # 归一化键，兼容重音/标点
    rosters={}; meta={}; cur=None; cur_name=None; group=None; heads_seen=0; missing={}
    for line in wt.splitlines():
        s=line.strip()
        g=re.match(r"^==\s*Group\s+([A-L])\s*==\s*$", s, re.I)   # 组标题
        if g: group=g.group(1).upper(); continue
        h=re.match(r"^={3,4}\s*(.+?)\s*={3,4}\s*$", s)           # ===Country===
        if h:
            heads_seen+=1
            cur_name=_delink(h.group(1)); cur=code_n.get(norm(cur_name))
            if cur: meta[cur]={"en":cur_name,"group":group}
            continue
        if "nat fs" in s.lower() and "player" in s.lower():
            p=parse_player_line(s)
            if not p: continue
            if cur: rosters.setdefault(cur, []).append(p)
            elif cur_name: missing[cur_name]=missing.get(cur_name,0)+1
    print(f"  （解析到 {heads_seen} 个三级标题，命中 {len(rosters)} 队）")
    if missing:
        print("  ⚠ 仍未匹配：" + " / ".join(f"{k}({v}人)" for k,v in missing.items()))
    return rosters, meta

def zh_review():
    """导出所有 中文名 清单到 data/zh_review.tsv，并列出仍无中文名的球员。"""
    import glob
    rows=[]; nozh=[]
    for fp in sorted(glob.glob("data/squads/*.json")):
        code=Path(fp).stem
        for p in json.load(open(fp,encoding="utf-8"))["players"]:
            en,zh=p["en"],p.get("zh","")
            rows.append(f"{code}\t{en}\t{zh}")
            if not zh or zh==en or re.search(r"[A-Za-z]",zh): nozh.append(f"{code}  {en}  → {zh}")
    Path("data/zh_review.tsv").write_text("\n".join(rows))
    print(f"已导出 {len(rows)} 条 → data/zh_review.tsv")
    print(f"仍无中文名 {len(nozh)} 条（冷门球员，Wikidata 无词条）：")
    for x in nozh[:40]: print("  ",x)

def dump_wikitext():
    """调试用：把名单页 wikitext 存到 wikitext_dump.txt，发我看真实模板格式。"""
    wt = requests.get(SQUADS_PAGE, params={
        "action":"parse","page":"2026_FIFA_World_Cup_squads","prop":"wikitext","format":"json",
    }, headers=UA, timeout=40).json()["parse"]["wikitext"]["*"]
    Path("wikitext_dump.txt").write_text(wt)
    print("已存 wikitext_dump.txt（约", len(wt), "字符）。把开头 ~60 行发我即可。")

# --------------------------------------------------------------------------- #
# 2) 完整赛季数据：FBref（soccerdata）
# --------------------------------------------------------------------------- #
def fbref_rows() -> list:
    import soccerdata as sd
    rows=[]
    for lg in LEAGUES:
        try:
            fb=sd.FBref(leagues=lg, seasons=SEASON)
            df=fb.read_player_season_stats(stat_type="standard").reset_index()
            for _,r in df.iterrows():
                rows.append({"name":str(r.get("player","")),
                    "year":(int(r["born"]) if str(r.get("born","")).isdigit() else None),
                    "app":int(r.get(("Playing Time","MP"),r.get("MP",0)) or 0),
                    "g":int(r.get(("Performance","Gls"),r.get("Gls",0)) or 0),
                    "a":int(r.get(("Performance","Ast"),r.get("Ast",0)) or 0)})
        except Exception as e:
            print(f"  ! FBref {lg}: {e}", file=sys.stderr)
        time.sleep(2)
    return rows

# --------------------------------------------------------------------------- #
# 2b) 完整赛季数据：Sofascore（直连非官方 API；覆盖广、较少被拦）
#     逐球员：搜索→取最近 25/26 联赛赛季的 出场/进球/助攻/评分。带磁盘缓存可续跑。
#     首次运行若字段对不上，把报错发我，按实际返回结构微调即可。
# --------------------------------------------------------------------------- #
SOFA="https://api.sofascore.com/api/v1"
SOFA_HDR={"User-Agent":"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
          "Referer":"https://www.sofascore.com/", "Accept":"application/json"}
def _sofa_raw(url):
    """Sofascore 用 TLS 指纹反爬，优先用 curl_cffi 模拟 Chrome；未安装则回退 requests。"""
    try:
        from curl_cffi import requests as creq
        r=creq.get(url, headers=SOFA_HDR, timeout=20, impersonate="chrome")
        return r.status_code, r.text
    except ImportError:
        r=requests.get(url, headers=SOFA_HDR, timeout=20)
        return r.status_code, r.text
def _sget(url, tries=2):
    last=None
    for i in range(tries):
        st,txt=_sofa_raw(url)
        if st==200:
            return json.loads(txt)
        last=Exception(f"HTTP {st}")
        time.sleep(1.2*(i+1))
    raise last
def sofa_player_season(name, year, cache):
    key=f"{name}|{year}"
    c=cache.get(key)
    if c is not None and "foot" in c: return c          # 旧缓存(无惯用脚/身价)自动重查升级
    res=None; ok=True
    try:
        js=_sget(f"{SOFA}/search/all?q={requests.utils.quote(name)}&page=0")
        cands=[r.get("entity",{}) for r in js.get("results",[]) if r.get("type")=="player"] \
              or js.get("players",[])
        pid=None
        for c in cands:
            dob=c.get("dateOfBirthTimestamp")
            if year and dob and time.gmtime(dob).tm_year==year: pid=c.get("id"); break
        if pid is None and cands: pid=cands[0].get("id")
        if pid:
            detail={}
            try:                                       # 球员详情：惯用脚/身价/身高/号码
                d=_sget(f"{SOFA}/player/{pid}").get("player",{})
                mv=d.get("proposedMarketValueRaw") or {}
                detail={"foot":{"Right":"右","Left":"左","Both":"双"}.get(d.get("preferredFoot")),
                        "val":(round(mv["value"]/1_000_000) if isinstance(mv,dict) and mv.get("value") else None),
                        "ht":d.get("height") or None,"num":d.get("jerseyNumber") or None}
            except Exception: pass
            seasons=_sget(f"{SOFA}/player/{pid}/statistics/seasons").get("uniqueTournamentSeasons",[])
            best=None
            for ut in seasons[:4]:                  # 只看前4项（按相关性排，第1个通常是主联赛）
                utid=ut.get("uniqueTournament",{}).get("id")
                for s in ut.get("seasons",[])[:2]:
                    if not any(t in str(s.get("year","")) for t in ("25","26")): continue
                    try:
                        ov=_sget(f"{SOFA}/player/{pid}/unique-tournament/{utid}/season/{s['id']}/statistics/overall").get("statistics",{})
                    except Exception: continue
                    ap=ov.get("appearances") or ov.get("matchesStarted") or 0
                    if best is None or ap>best["app"]:
                        best={"app":ap,"g":ov.get("goals",0) or 0,"a":ov.get("assists",0) or 0,
                              "rt":round(float(ov.get("rating",0) or 0),2)}
                if best and best["app"]>=15: break  # 已拿到主联赛量级的数据，提前收手
            res={**detail, **(best or {})}
            if not any(v is not None for v in res.values()): res=None
    except Exception as e:
        ok=False                                   # 失败不写缓存，下次重试
        print(f"  ! sofa {name}: {type(e).__name__}: {e}", file=sys.stderr)
    if ok: cache[key]=res
    time.sleep(0.4); return res

def sofa_extra_test(name="Lionel Messi"):
    """调试：看 Sofascore 球员详情里有没有 惯用脚/身价/身高。"""
    import urllib.parse
    js=_sget(f"{SOFA}/search/all?q={urllib.parse.quote(name)}&page=0")
    pl=[x.get("entity",{}) for x in js.get("results",[]) if x.get("type")=="player"]
    if not pl: print("无 player 结果"); return
    pid=pl[0]["id"]; print("player id:",pid)
    d=_sget(f"{SOFA}/player/{pid}").get("player",{})
    print("preferredFoot:", d.get("preferredFoot"))
    print("height:", d.get("height"))
    print("proposedMarketValueRaw:", d.get("proposedMarketValueRaw"))
    print("marketValueCurrency:", d.get("proposedMarketValueRaw",{}))
    print("shirtNumber:", d.get("jerseyNumber"))
    print("全部 key:", list(d.keys()))

def sofa_test(name="Lionel Messi"):
    """调试：打印 Sofascore 三步请求的原始返回，把输出贴给 Claude 修接口。"""
    import urllib.parse
    try: import curl_cffi; print("curl_cffi: 已安装 ✓")
    except ImportError: print("curl_cffi: 未安装 ✗（请先 python3 -m pip install curl_cffi）")
    url=f"{SOFA}/search/all?q={urllib.parse.quote(name)}&page=0"
    print("GET",url)
    st,txt=_sofa_raw(url)
    print("status:",st); print("body[:400]:",txt[:400])
    if st!=200: return
    js=json.loads(txt); print("keys:",list(js.keys()))
    rs=js.get("results",[])
    print("result types:",[x.get("type") for x in rs][:8])
    pl=[x.get("entity",{}) for x in rs if x.get("type")=="player"]
    if not pl: print("!! 无 player 结果"); return
    p=pl[0]; pid=p.get("id"); print("first player:",pid,p.get("name"),p.get("dateOfBirthTimestamp"))
    st2,txt2=_sofa_raw(f"{SOFA}/player/{pid}/statistics/seasons")
    print("seasons status:",st2); print("seasons body[:300]:",txt2[:300])
    if st2!=200: return
    uts=json.loads(txt2).get("uniqueTournamentSeasons",[])
    print("tournaments:",[(u.get("uniqueTournament",{}).get("name"),
        [s.get("year") for s in u.get("seasons",[])[:3]]) for u in uts[:5]])
    if uts:
        ut=uts[0]; utid=ut["uniqueTournament"]["id"]; s=ut["seasons"][0]
        st3,txt3=_sofa_raw(f"{SOFA}/player/{pid}/unique-tournament/{utid}/season/{s['id']}/statistics/overall")
        print("stats status:",st3); print("stats body[:400]:",txt3[:400])

# --------------------------------------------------------------------------- #
# 3) 身价 / 号码 / 身高 / 脚：Transfermarkt（ScraperFC）
# --------------------------------------------------------------------------- #
TM_BASE="https://www.transfermarkt.com"
TM_HDR={"User-Agent":"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept-Language":"en-US,en;q=0.9"}
TM_VEREIN={"ARG":"3437"}     # 国家队 verein id（先放 ARG 验证，跑通后再补全 48）
def _tm_raw(url):
    try:
        from curl_cffi import requests as creq
        r=creq.get(url, headers=TM_HDR, timeout=25, impersonate="chrome")
        return r.status_code, r.text
    except ImportError:
        r=requests.get(url, headers=TM_HDR, timeout=25)
        return r.status_code, r.text

def tm_test(code="ARG"):
    """调试：抓德转国家队阵容页，打印一名球员行附近的原始 HTML，贴给 Claude 写解析。"""
    try: import curl_cffi; print("curl_cffi: 已安装 ✓")
    except ImportError: print("curl_cffi: 未安装 ✗")
    vid=TM_VEREIN.get(code)
    if not vid: print(f"无 {code} 的 verein id"); return
    url=f"{TM_BASE}/x/kader/verein/{vid}/saison_id/2025/plus/1"
    print("GET",url)
    st,html=_tm_raw(url)
    print("status:",st,"| html length:",len(html))
    if st!=200: print("body[:300]:",html[:300]); return
    import re as _re
    # 找第一处身价（含 € 的 hauptlink 单元格）周围的 HTML
    m=_re.search(r"Messi|Lautaro|Álvarez|Alvarez", html)
    i=m.start() if m else (html.find("hauptlink") if "hauptlink" in html else 0)
    print("----- HTML 片段（球员行上下文）-----")
    print(html[max(0,i-1800):i+600])

def tm_rows(code:str) -> list:
    """返回 [{'name','year','val'(€M),'num','ht','foot'}]。按 ScraperFC 实际 API 调整。"""
    try:
        import ScraperFC as sfc
        tm=sfc.Transfermarkt()
        df=tm.scrape_players(f"{code} national team")   # 或直接传国家队页 URL
        out=[]
        for _,r in df.iterrows():
            out.append({"name":str(r.get("Name","")),
                "year":None,
                "val":_eurM(r.get("Market value")),
                "num":_int(r.get("Number")),
                "ht":_int(re.sub(r"\D","",str(r.get("Height","")))[:3] or 0),
                "foot":{"left":"左","right":"右"}.get(str(r.get("Foot","")).lower())})
        return out
    except Exception as e:
        print(f"  ! TM {code}: {e}", file=sys.stderr)
        return []
def _eurM(v):
    s=str(v).lower().replace("€","").strip()
    if "m" in s: return round(float(re.sub(r"[^0-9.]","",s)))
    if "k" in s: return round(float(re.sub(r"[^0-9.]","",s))/1000,1)
    return None
def _int(v):
    try: return int(re.sub(r"\D","",str(v)))
    except: return None

# --------------------------------------------------------------------------- #
# 4) Wikidata：中文名 + 身高(P2048) + 体重(P2067)，按出生日期消歧义
# --------------------------------------------------------------------------- #
def wikidata_player(en, born, cache):
    c=cache.get(en)
    if c and any(v is not None for v in c.values()):   # 全空的缓存视为可重查（可能是上次网络失败）
        return c
    res={"zh":None,"ht":None,"wt":None}; ok=True
    try:
        sr=_get(WIKIDATA_API,params={"action":"wbsearchentities","search":en,
            "language":"en","type":"item","format":"json","limit":5},timeout=15,tries=3
            ).json().get("search") or []
        ids=[x["id"] for x in sr]
        if ids:
            ents=_get(WIKIDATA_API,params={"action":"wbgetentities","ids":"|".join(ids),
                "props":"labels|claims","languages":"zh-cn|zh-hans|zh","format":"json"},timeout=20,tries=3
                ).json().get("entities",{})
            def dob(e):
                try: return e["claims"]["P569"][0]["mainsnak"]["datavalue"]["value"]["time"][1:5]
                except Exception: return None
            pick=None
            for qid in ids:                                   # 1) 生年匹配
                e=ents.get(qid,{})
                if born and dob(e)==born[:4]: pick=e; break
            if pick is None:                                  # 2) 否则取第一个“足球运动员”
                for qid in ids:
                    e=ents.get(qid,{})
                    occ=[c.get("mainsnak",{}).get("datavalue",{}).get("value",{}).get("id")
                         for c in e.get("claims",{}).get("P106",[])]
                    if "Q937857" in occ: pick=e; break
            if pick:
                lab=pick.get("labels",{})
                res["zh"]=(lab.get("zh-cn") or lab.get("zh-hans") or lab.get("zh") or {}).get("value")
                def qty(prop):
                    try:
                        amt=float(pick["claims"][prop][0]["mainsnak"]["datavalue"]["value"]["amount"])
                        if prop=="P2048" and amt<3: amt*=100   # 米 → 厘米
                        return round(amt)
                    except Exception: return None
                res["ht"]=qty("P2048"); res["wt"]=qty("P2067")
    except Exception as e:
        ok=False                                   # 失败不写缓存，下次重试
        print(f"  ! wikidata {en}: {type(e).__name__}", file=sys.stderr)
    if ok: cache[en]=res
    time.sleep(0.3); return res

# --------------------------------------------------------------------------- #
# 5) 真实赛程：Wikipedia 赛程页，解析 {{Football box ...}} 模板，产出 data/fixtures.js
# --------------------------------------------------------------------------- #
FIX_PAGES=["2026 FIFA World Cup group stage","2026 FIFA World Cup knockout stage"]
FIX_CANDIDATES=["2026 FIFA World Cup group stage","2026 FIFA World Cup",
    "2026 FIFA World Cup knockout stage","2026 FIFA World Cup Group A",
    "2026 FIFA World Cup statistics"]
def _wikitext(page):
    try:
        js=_get(SQUADS_PAGE, params={"action":"parse","page":page,"redirects":"1",
            "prop":"wikitext","format":"json"}, timeout=40).json()
        if "parse" not in js: return ""          # 页面不存在 → API 返回 error
        return js["parse"]["wikitext"]["*"]
    except Exception as e:
        print(f"  ! 拉取 {page} 失败: {type(e).__name__}", file=sys.stderr); return ""

def _team_code(name, mycodes, code_n):
    name=re.sub(r"\[\[(?:[^|\]]*\|)?|\]\]|'","",name or "").strip()
    if name.upper() in mycodes: return name.upper()
    return code_n.get(norm(name))

# 轮次中文名 + 场馆时区（夏令时：美东-4 美中-5 美山-6 美西-7；墨西哥-6 无夏令时）
RND_ZH={"round of 32":"1/16决赛","round of 16":"1/8决赛","quarter-finals":"1/4决赛",
        "quarterfinals":"1/4决赛","semi-finals":"半决赛","semifinals":"半决赛",
        "third place play-off":"季军赛","third place playoff":"季军赛","final":"决赛"}
TZOFF=[("azteca",-6),("akron",-6),("bbva",-6),("metlife",-4),("gillette",-4),
       ("lincoln financial",-4),("hard rock",-4),("mercedes-benz",-4),("at&t",-5),
       ("nrg",-5),("arrowhead",-5),("sofi",-7),("levi",-7),("lumen",-7),
       ("bc place",-7),("bmo",-4)]
def _bj(date, h, mi, venue):
    """场馆当地时间 → 北京时间（返回 北京日期, 北京HH:MM）"""
    from datetime import datetime, timedelta
    off=next((v for k,v in TZOFF if k in (venue or "").lower()), -5)
    y,mo,da=map(int,date.split("-"))
    dt=datetime(y,mo,da,h,mi)+timedelta(hours=8-off)
    return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")

def _parse_match_section(head, body, mycodes, code_n):
    """从 '=== A vs B ===' 标题 + 正文提取 队/日期/北京时间/场馆/比分。"""
    dm=re.search(r"\{\{\s*[Ss]tart date[^}]*?\|\s*(\d{4})\s*\|\s*(\d{1,2})\s*\|\s*(\d{1,2})"
                 r"(?:\s*\|\s*(\d{1,2})\s*\|\s*(\d{1,2}))?", body)
    if not dm: return None
    date=f"{int(dm.group(1)):04d}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}"
    h=mi=None
    if dm.group(4) and dm.group(5):                       # Start date 自带 24h 时间
        h,mi=int(dm.group(4)),int(dm.group(5))
    else:                                                 # 否则找正文里的 12 小时制（美式 1:00 p.m.）
        m12=re.search(r"\b(\d{1,2}):(\d{2})\s*(?:&nbsp;|\s)*([ap])\.?\s*\.?m\b", body, re.I)
        if m12:
            h=int(m12.group(1))%12+(12 if m12.group(3).lower()=="p" else 0); mi=int(m12.group(2))
    vm=re.search(r"\[\[([^\]|]*(?:Stadium|Arena|Field|Park|Place|Estadio|Bowl)[^\]|]*)(?:\|[^\]]+)?\]\]", body)
    ven=vm.group(1).strip() if vm else ""
    tm=""
    if h is not None:
        date,tm=_bj(date,h,mi,ven)                        # 统一转北京时间
    sm=re.search(r"team1score\s*=\s*(\d+)[^}]*?team2score\s*=\s*(\d+)", body, re.S)
    score=[int(sm.group(1)),int(sm.group(2))] if sm else None
    t1=t2=None
    if " vs " in head:
        a,b=head.split(" vs ",1)
        t1=_team_code(a,mycodes,code_n); t2=_team_code(b,mycodes,code_n)
    return {"home":t1,"away":t2,"date":date,"time":tm,"venue":ven,"score":score,
            "label":_delink(head)}

def _matches_from_page(page, default_round, mycodes, code_n):
    """default_round 为 None 时从 ==二级标题== 推断轮次（淘汰赛页）。"""
    wt=_wikitext(page)
    if not wt: return []
    h2s=[(m.start(), m.group(1).strip()) for m in re.finditer(r"\n==([^=\n][^\n]*?)==\s*\n", wt)]
    h3s=[(m.start(), m.end(), m.group(1).strip()) for m in re.finditer(r"\n===\s*([^=\n]+?)\s*===\s*\n", wt)]
    res=[]
    for i,(s,e,head) in enumerate(h3s):
        body=wt[e: h3s[i+1][0] if i+1<len(h3s) else len(wt)]
        mm=_parse_match_section(head, body, mycodes, code_n)
        if not mm: continue
        if default_round is not None:
            mm["round"]=default_round
        else:
            prior=[h for hp,h in h2s if hp<s]
            rname=prior[-1] if prior else ""
            mm["round"]=RND_ZH.get(rname.lower().strip(), rname)
        res.append(mm)
    return res

# --- Sofascore 首发阵容：赛前约1小时放出确认首发；写入 fixtures 的 lineups 字段 ---
def _sofa_event_index(dates):
    """取这些日期的 Sofascore 赛事，过滤世界杯，返回 {(homecode,awaycode): eventId}。"""
    code_n={norm(k):v for k,v in NAME2CODE.items()}; mycodes=set(NAME2CODE.values())
    def code_of(nm):
        nm=(nm or "").strip()
        return nm.upper() if nm.upper() in mycodes else code_n.get(norm(nm))
    idx={}
    for d in sorted(set(dates)):
        try: evs=_sget(f"{SOFA}/sport/football/scheduled-events/{d}").get("events",[])
        except Exception as e:
            print(f"  ! sofa 赛事 {d}: {type(e).__name__}", file=sys.stderr); continue
        for ev in evs:
            t=ev.get("tournament") or {}
            nm=((t.get("uniqueTournament") or {}).get("name") or "")+" "+(t.get("name") or "")
            if "world cup" not in nm.lower(): continue   # 只要世界杯，排除其它赛事
            hc=code_of((ev.get("homeTeam") or {}).get("name"))
            ac=code_of((ev.get("awayTeam") or {}).get("name"))
            if hc and ac and ev.get("id"): idx[(hc,ac)]=ev["id"]
        time.sleep(0.3)
    return idx

def sofa_lineup(eid):
    """返回 {'confirmed':bool,'home':[号码...],'away':[号码...]}；无首发则 None。"""
    js=_sget(f"{SOFA}/event/{eid}/lineups")
    def starters(side):
        out=[]
        for p in ((js.get(side) or {}).get("players") or []):
            if p.get("substitute"): continue             # 只要首发（非替补）
            n=p.get("shirtNumber") or (p.get("player") or {}).get("jerseyNumber")
            try: out.append(int(n))
            except (TypeError,ValueError): pass
        return out
    lu={"confirmed":bool(js.get("confirmed")),"home":starters("home"),"away":starters("away")}
    return lu if (lu["home"] or lu["away"]) else None

def attach_lineups(fixtures):
    """给近两天内、未结束的比赛附上 Sofascore 首发（号码列表）。失败不影响赛程。"""
    from datetime import datetime, timedelta
    def d(s):
        try: return datetime.strptime(s,"%Y-%m-%d").date()
        except Exception: return None
    today=datetime.utcnow().date()
    todo=[m for m in fixtures if m.get("home") and m.get("away") and m.get("status")!="FT"
          and d(m.get("date")) and (today-timedelta(days=1))<=d(m["date"])<=(today+timedelta(days=2))]
    if not todo: return
    qdates=set()                                          # Sofascore 按 UTC 日期分组，前后各扩一天
    for m in todo:
        dt=d(m["date"])
        for k in (-1,0,1): qdates.add((dt+timedelta(days=k)).strftime("%Y-%m-%d"))
    idx=_sofa_event_index(qdates); n=0
    for m in todo:
        eid=idx.get((m["home"],m["away"]))
        if not eid: continue
        try: lu=sofa_lineup(eid)
        except Exception as e:
            print(f"  ! sofa 首发 {m['home']}-{m['away']}: {type(e).__name__}", file=sys.stderr); continue
        if lu: m["lineups"]=lu; n+=1
        time.sleep(0.4)
    if n: print(f"  ✓ 首发 {n} 场 → lineups")

def fetch_fixtures():
    mycodes=set(NAME2CODE.values()); code_n={norm(k):v for k,v in NAME2CODE.items()}
    out=[]; mid=0
    for L in "ABCDEFGHIJKL":                       # 12 个小组页
        for m in _matches_from_page(f"2026 FIFA World Cup Group {L}", f"{L}组", mycodes, code_n):
            if not (m["home"] and m["away"]): continue
            m["id"]="m"+str(mid); m["status"]="FT" if m["score"] else ""; m.pop("label",None)
            out.append(m); mid+=1
    # 淘汰赛：对阵已定 → 正常场次；未定 → 「待定」占位（带轮次/日期/北京时间）
    for m in _matches_from_page("2026 FIFA World Cup knockout stage", None, mycodes, code_n):
        if m["home"] and m["away"]:
            m["id"]="m"+str(mid); m["status"]="FT" if m["score"] else ""; m.pop("label",None)
        else:
            m={"id":"k"+str(mid),"ko":True,"date":m["date"],"time":m["time"],
               "round":m["round"],"label":m["label"]}
        out.append(m); mid+=1
    try: attach_lineups(out)                       # 附上临近比赛的真实/预测首发
    except Exception as e: print(f"  ! 首发抓取跳过: {type(e).__name__}: {e}", file=sys.stderr)
    return out or None

def fixtures_test():
    mycodes=set(NAME2CODE.values()); code_n={norm(k):v for k,v in NAME2CODE.items()}
    ms=_matches_from_page("2026 FIFA World Cup Group A", "A组", mycodes, code_n)
    print(f"A 组解析到 {len(ms)} 场（时间已转北京时间）：")
    for m in ms: print("  ",m["date"],m["time"] or "--:--",m["home"],"vs",m["away"],"@",m["venue"])
    ko=_matches_from_page("2026 FIFA World Cup knockout stage", None, mycodes, code_n)
    print(f"\n淘汰赛解析到 {len(ko)} 场，前 6 场：")
    for m in ko[:6]: print("  ",m["round"],"|",m["date"],m["time"] or "--:--","|",
                           (m["home"],m["away"]) if m["home"] and m["away"] else m["label"])

# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main(only=None, season=True, wiki=True):
    SQ.mkdir(parents=True, exist_ok=True)
    overrides=json.loads(OVERRIDES.read_text()) if OVERRIDES.exists() else {}
    overrides_n={norm(k):v for k,v in overrides.items()}   # 归一化键：忽略重音差异
    CLUBSZH=OUT/"clubs_zh.json"
    clubs_zh=json.loads(CLUBSZH.read_text()) if CLUBSZH.exists() else {}
    WCACHE=OUT/"wiki_cache.json"
    wcache=json.loads(WCACHE.read_text()) if (wiki and WCACHE.exists()) else {}
    unmatched=[]

    print("· 抓最终名单 …")
    rosters, tmeta=fetch_rosters()
    print(f"  得到 {len(rosters)} 队")
    if only:
        rosters={k:v for k,v in rosters.items() if k==only}
        print(f"  仅处理 {only}（单队验证模式）")
    OUT.joinpath("teams_meta.json").write_text(json.dumps(tmeta,ensure_ascii=False,indent=2))
    bygrp={}
    for c,m in tmeta.items(): bygrp.setdefault(m.get("group") or "?", []).append(c)
    print("  分组（已写 data/teams_meta.json，请把下面整段发我）：")
    for g in sorted(bygrp): print(f"    {g}: {' '.join(sorted(bygrp[g]))}")

    print("· Wikidata：补 中文名/身高/体重 …（逐球员，带缓存可续跑）" if wiki else "· 跳过 Wikidata")
    print("· 赛季数据用 Sofascore（逐球员，带缓存可续跑）…" if season
          else "· 跳过赛季数据（出场/球/助留空）")
    SCACHE=OUT/"sofa_cache.json"
    scache=json.loads(SCACHE.read_text()) if (season and SCACHE.exists()) else {}

    manifest={}; combined={}
    SJS=OUT/"squads.js"
    if SJS.exists():                                  # 合并模式：保留已有各队，单队运行不丢数据
        m=re.match(r"window\.REAL_SQUADS=(.*);\s*$", SJS.read_text().strip(), re.S)
        if m:
            try: combined=json.loads(m.group(1))
            except Exception: combined={}
    for code,players in rosters.items():
        old={x["en"]:x for x in combined.get(code,[])}   # 上次的数据，用于累加合并
        out=[]
        for p in players:
            wd=wikidata_player(p["en"], p["born"], wcache) if wiki else None
            zh=overrides_n.get(norm(p["en"])) or _simp((wd or {}).get("zh")) or p["en"]
            if wiki and zh==p["en"]: unmatched.append(("zh",code,p["en"]))
            f=sofa_player_season(p["en"],p["year"],scache) if season else None
            if season and f is None: unmatched.append(("season",code,p["en"]))
            f=f or {}
            rec={"num":p["num"] or f.get("num"),"en":p["en"],"zh":zh,
                "pos":p["pos"],"born":p["born"],
                "ht":(wd or {}).get("ht") or f.get("ht"),
                "wt":(wd or {}).get("wt"),"foot":f.get("foot"),
                "club":p["club"],"clubZh":clubs_zh.get(p["club"]),
                "caps":p["caps"],"intl":p["intl"],
                "app":f.get("app"),"g":f.get("g"),"a":f.get("a"),"rt":f.get("rt"),
                "val":f.get("val"),**({"cap":1} if p["cap"] else {})}
            # 累加合并：本次没抓到的字段，沿用上次已有的，避免清空
            o=old.get(p["en"])
            if o:
                for k in ("ht","wt","foot","app","g","a","rt","val","num","clubZh"):
                    if rec.get(k) is None and o.get(k) is not None: rec[k]=o[k]
                if rec["zh"]==p["en"] and o.get("zh") and o["zh"]!=p["en"]: rec["zh"]=o["zh"]
            out.append(rec)
        (SQ/f"{code}.json").write_text(json.dumps({"players":out},ensure_ascii=False,indent=2))
        if season: SCACHE.write_text(json.dumps(scache,ensure_ascii=False))   # 每队存一次，方便续跑
        if wiki:   WCACHE.write_text(json.dumps(wcache,ensure_ascii=False))
        combined[code]=out
        OUT.joinpath("squads.js").write_text(                                 # 即时写，中断也有
            "window.REAL_SQUADS="+json.dumps(combined,ensure_ascii=False)+";\n")
        print(f"    {code} ✓ ({len(out)}人)")
        gk=sum(1 for x in out if x["pos"]=="GK")
        manifest[code]={"n":len(out),"gk":gk,"ok":23<=len(out)<=26 and gk>=3}

    OUT.joinpath("squads.js").write_text(
        "window.REAL_SQUADS="+json.dumps(combined,ensure_ascii=False)+";\n")
    OUT.joinpath("manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    if unmatched:
        Path("unmatched.log").write_text("\n".join("\t".join(map(str,u)) for u in unmatched))
    if not only:                                  # 顺带抓真实赛程
        print("· 抓真实赛程（openfootball）…")
        fx=fetch_fixtures()
        if fx:
            OUT.joinpath("fixtures.js").write_text("window.REAL_FIXTURES="+json.dumps(fx,ensure_ascii=False)+";\n")
            print(f"  ✓ 赛程 {len(fx)} 场 → data/fixtures.js")
    bad=[c for c,m in manifest.items() if not m["ok"]]
    print(f"✓ 完成 {len(combined)} 队 → data/squads/*.json + data/squads.js")
    if bad:  print(f"  ⚠ 人数异常队（需检查）：{bad}")
    if unmatched: print(f"  ⚠ {len(unmatched)} 项名字未匹配 → unmatched.log")

if __name__=="__main__":
    args=[a for a in sys.argv[1:]]
    if "dump" in args:
        dump_wikitext()
    elif args and args[0]=="zh-review":
        zh_review()
    elif args and args[0]=="sofa-test":
        sofa_test(" ".join(args[1:]) or "Lionel Messi")
    elif args and args[0]=="tm-test":
        tm_test((args[1].upper() if len(args)>1 else "ARG"))
    elif args and args[0]=="sofa-extra-test":
        sofa_extra_test(" ".join(args[1:]) or "Lionel Messi")
    elif args and args[0]=="fixtures-test":
        fixtures_test()
    elif args and args[0]=="lineup-test":         # 用法: build_all.py lineup-test 2026-06-12
        ds=args[1:] or [time.strftime("%Y-%m-%d")]
        idx=_sofa_event_index(ds); print("世界杯赛事 →", idx)
        for k,eid in list(idx.items())[:3]: print(k, eid, "→", sofa_lineup(eid))
    elif args and args[0]=="fixtures":            # 只更新赛程，不动名单
        fx=fetch_fixtures()
        if fx:
            OUT.mkdir(parents=True,exist_ok=True)
            (OUT/"fixtures.js").write_text("window.REAL_FIXTURES="+json.dumps(fx,ensure_ascii=False)+";\n")
            print(f"✓ 赛程 {len(fx)} 场 → data/fixtures.js")
    else:
        # 模式：fast=只名单 | wiki=名单+Wikidata(中文名/身高/体重) | 默认=全量
        low=[a.lower() for a in args]
        mode="fast" if "fast" in low else ("wiki" if "wiki" in low else "full")
        code=next((a.upper() for a in args if a.lower() not in ("fast","wiki") and len(a)==3), None)
        main(only=code, season=(mode=="full"), wiki=(mode in ("wiki","full")))
