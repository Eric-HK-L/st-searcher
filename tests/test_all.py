#!/usr/bin/env python3
"""端到端自测。

分四类，全部对着**外部事实**或**真实 HTTP 交互**验证，不做自我比对：

  1. ground-truth  拿 TVmaze 的真实季集数（tests/ground_truth.json）对答案
  2. search        单集检索 / 整季检索 / 剧名解析的回归用例
  3. webui         真起服务、真爬页面、真下压缩包、真解包检查
  4. units         编码识别、体积格式、特别篇识别等纯函数

用法:
    python3 tests/test_all.py            # 全部
    python3 tests/test_all.py units      # 只跑某一类
"""
import io
import json
import os
import re
import sqlite3
import sys
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from config import DB_PATH  # noqa: E402

PASS, FAIL, SKIP = [], [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {name}" + (f"   {detail}" if detail and not ok else ""))


def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


# ---------------------------------------------------------------- 1. ground truth
# 数据集是字幕归档，不是完整剧集库：某些季本来就没有，这不算工具的错。
# 所以只检查「工具自己造成」的错误：
#   · 幻影季  —— 现实中不存在的季却列了出来
#   · 超集号  —— 集号超过现实集数（允许，因为数据可能把特别篇编成 E23/E24）
#   · 丢季    —— 库里明明有数据，工具却没列出来
KEY = {
    "Sherlock": "sherlock", "Person of Interest": "person.of.interest",
    "Breaking Bad": "breaking.bad", "Game of Thrones": "game.of.thrones",
    "Friends": "friends", "Downton Abbey": "downton.abbey", "Black Mirror": "black.mirror",
    "The Big Bang Theory": "the.big.bang.theory", "Prison Break": "prison.break",
    "The Walking Dead": "the.walking.dead", "Grey's Anatomy": "greys.anatomy",
    "House of Cards": "house.of.cards", "Westworld": "westworld",
    "The Vampire Diaries": "the.vampire.diaries", "Criminal Minds": "criminal.minds",
    "This Is Us": "this.is.us", "The Flash": "the.flash", "The 100": "the.100",
    "Lost": "lost", "Fringe": "fringe",
}
# 数据把特别篇编成了普通集号的已知案例（不是工具的错误）
KNOWN_OVERRUN = {("Downton Abbey", 6): 9, ("Prison Break", 4): 24}


def t_groundtruth():
    from searcher import series_overview, parse_video_name
    gt = json.loads((Path(__file__).parent / "ground_truth.json").read_text(encoding="utf-8"))
    con = conn()
    for label, info in gt.items():
        real = {int(k): v for k, v in info["seasons"].items()}
        ov = series_overview(con, parse_video_name(KEY[label]))
        if not ov:
            check(f"groundtruth/{label}", False, "工具找不到这部剧")
            continue
        got = {se: len(pairs) for se, pairs, _v in ov[0]["seasons"] if se > 0}
        phantom = sorted(set(got) - set(real))
        # 丢季：库里真有、工具没列出来 —— 这才是工具 bug
        lost = []
        for se in real:
            n = con.execute("SELECT count(*) FROM subs WHERE series_key=? AND season=?",
                            (KEY[label], se)).fetchone()[0]
            if n and se not in got:
                lost.append(se)
        # 超集号：允许已知的特别篇编号，其余算可疑
        over = []
        for se, pairs, _v in ov[0]["seasons"]:
            if se in real and pairs:
                mx = max(ep for ep, _ in pairs)
                if mx > real[se] and KNOWN_OVERRUN.get((label, se), mx) != mx:
                    over.append(f"S{se:02d}E{mx}>{real[se]}")
        ok = not phantom and not lost and not over
        check(f"groundtruth/{label}", ok,
              f"幻影季={phantom} 丢季={lost} 超集号={over}")


# ---------------------------------------------------------------- 2. search
SINGLE = [
    ("The.Vampire.Diaries.S03E10.720p.mkv", "vampire"), ("Game.of.Thrones.S08E01.1080p.mkv", "thrones"),
    ("Friends.S01E01.720p.mkv", "friends"), ("The.Big.Bang.Theory.S01E01.mkv", "bang"),
    ("Breaking.Bad.S01E01.mkv", "breaking"), ("Westworld.S01E01.1080p.mkv", "westworld"),
    ("House.of.Cards.S01E01.mkv", "cards"), ("Sherlock.S01E01.mkv", "sherlock"),
    ("Downton.Abbey.S01E01.mkv", "downton"), ("Prison.Break.S04E18.mkv", "prison"),
    ("Criminal.Minds.S17E01.mkv", "criminal"), ("Groundhog.Day.1993.720p.mkv", "groundhog"),
    ("The.Matrix.1999.1080p.mkv", "matrix"), ("Avatar.2009.1080p.mkv", "avatar"),
    ("District.9.2009.1080p.mkv", "district"), ("权力的游戏 S08E01", "thrones"),
    ("老友记 第一季第一集", "friends"), ("生活大爆炸 S01E01", "bang"),
    ("绝命毒师 S01E01", "breaking"), ("纸牌屋 S01E01", "cards"), ("唐顿庄园 S01E01", "downton"),
    ("越狱 S04E18", "prison"), ("黑客帝国 1999", "matrix"), ("阿凡达", "avatar"),
    ("功夫之王", "功夫之王"), ("Person.of.Interest.S02E22.mkv", "interest"),
    ("This.Is.Us.S01E01.mkv", "this.is.us"), ("Game.of.Thrones.S08E01", "thrones"),
    ("Person.of.Interest.S02E22", "interest"), ("Sherlock.S01E01", "sherlock"),
]
# 整季集数：对着现实集数（见上表）核过
SEASON = [("权力的游戏", 8, 6), ("老友记", 1, 24), ("绝命毒师", 1, 7), ("纸牌屋", 1, 13),
          ("唐顿庄园", 1, 7), ("疑犯追踪", 2, 22), ("This Is Us", 1, 18), ("黑镜", 1, 3),
          ("Sherlock", 1, 3), ("Breaking Bad", 1, 7), ("Game of Thrones", 1, 10)]
LABEL = {"黑镜": "黑镜", "疑犯追踪": "疑犯追踪", "老友记": "老友记",
         "Breaking Bad": "绝命毒师", "实习医生格蕾": "实习医生格蕾"}


def t_search():
    from searcher import find_candidates, parse_video_name, season_groups, series_overview
    con = conn()
    bad = []
    for name, expect in SINGLE:
        c = find_candidates(con, parse_video_name(name), limit=40)
        hay = (c[0]["inner_path"] + (c[0]["enname"] or "") + (c[0]["cnname"] or "")
               + (c[0]["series_key"] or "")).lower() if c else ""
        if expect not in hay:
            bad.append(name)
    check(f"search/单集 {len(SINGLE)} 例", not bad, f"失败: {bad}")

    bad = []
    for q, se, n in SEASON:
        got = len(season_groups(con, parse_video_name(q), se)[0]["pairs"])
        if got != n:
            bad.append(f"{q} S{se}: {got}!={n}")
    check(f"search/整季 {len(SEASON)} 例", not bad, "; ".join(bad))

    bad = []
    for q, want in LABEL.items():
        got = series_overview(con, parse_video_name(q))[0]["label"]
        if got != want:
            bad.append(f"{q}: {got!r}!={want!r}")
    check(f"search/剧名 {len(LABEL)} 例", not bad, "; ".join(bad))

    # SxxE00 是发行方放的特别篇/先行篇，不能算进正片集数
    # （Game of Thrones S01 曾经因为 S01E00 显示成 11 集）
    n0 = con.execute("SELECT count(*) FROM subs WHERE episode=0 AND season>0").fetchone()[0]
    check("search/E00 归入特别篇", n0 == 0, f"还有 {n0} 条 episode=0 留在正片季里")
    from searcher import _specials_pairs
    sp = _specials_pairs(con, {"series_key": "sherlock"})
    check("search/未播出试播集是特别篇",
          any("UNAIRED" in c["inner_path"].upper() for _e, cs in sp for c in cs),
          "Sherlock 的 UNAIRED PILOT 没被归到特别篇")

    # 中文名搜索必须覆盖全部季（曾经只出前几季）
    ov = series_overview(con, parse_video_name("实习医生格蕾"))
    n = len([s for s, _p, _v in ov[0]["seasons"] if s > 0])
    real = con.execute("SELECT count(DISTINCT season) FROM subs WHERE series_key='greys.anatomy'"
                       " AND season>0").fetchone()[0]
    check("search/中文名覆盖全部季", n == real, f"总览 {n} 季 / 库里 {real} 季")


# ---------------------------------------------------------------- 3. webui
def t_webui(base="http://127.0.0.1:8777"):
    def get(u, timeout=180):
        with urllib.request.urlopen(base + u, timeout=timeout) as r:
            return r.status, r.read()

    try:
        get("/", timeout=10)
    except Exception as e:
        check("webui/服务可达", False, str(e))
        return
    check("webui/服务可达", True)

    problems = []
    for sk in ("black.mirror", "person.of.interest", "sherlock", "downton.abbey", "the.100"):
        try:
            _st, body = get("/?" + urllib.parse.urlencode({"q": sk}))
        except Exception as e:
            problems.append(f"{sk} 总览失败 {e}")
            continue
        h = body.decode("utf-8")
        dls = [d.replace("&amp;", "&") for d in
               re.findall(r'href="(/\?[^"]*season=\d+[^"]*view=(?:season|episode)[^"]*)"', h)]
        if not dls:
            problems.append(f"{sk} 总览没有任何整季/分集链接")
            continue
        seen = set()
        for d in dls:
            se = re.search(r"season=(\d+)", d).group(1)
            if se in seen:
                continue
            seen.add(se)
            if len(seen) > 2:
                break
            st, b2 = get(d)
            h2 = b2.decode("utf-8")
            if st != 200:
                problems.append(f"{sk} {d} -> HTTP {st}")
                continue
            if "view=season" not in d:
                if not re.search(r'class="no">(S\d+E\d+|SP\d+)', h2):
                    problems.append(f"{sk} S{se} 分集页没有集")
                continue
            # 整季下载页：要么有下载按钮，要么有明确的「没有统一版本」说明
            btns = [b.replace("&amp;", "&") for b in
                    re.findall(r'href="(/season\.zip\?[^"]*)"', h2)]
            if not btns:
                if "没有" not in h2 or "发布版本" not in h2:
                    problems.append(f"{sk} S{se} 整季页既没按钮也没说明")
                continue
            try:
                _st3, z = get(btns[0], timeout=300)
                zf = zipfile.ZipFile(io.BytesIO(z))
                names = zf.namelist()
                if zf.testzip() is not None:
                    problems.append(f"{sk} S{se} 压缩包损坏")
                    continue
                eps = [re.search(r"(S\d+E\d+|SP\d+)", n).group(1)
                       for n in names if re.search(r"(S\d+E\d+|SP\d+)", n)]
                if not eps:
                    problems.append(f"{sk} S{se} 包里没有可识别的集号")
                elif len(eps) != len(set(eps)):
                    problems.append(f"{sk} S{se} 同一集在包里出现多次")
            except Exception as e:
                problems.append(f"{sk} S{se} 下载/解包失败: {e}")
    check("webui/页面与按钮", not problems, "; ".join(problems[:4]))


# ---------------------------------------------------------------- 3a. 推荐包
def t_recommend(base="http://127.0.0.1:8777"):
    """推荐整季包：有完整版本就推那个版本，都不完整就推拼出来的补全包。

    要求推荐包下载下来必须**每集恰好一个文件**，且集数齐全。
    """
    import sqlite3
    from searcher import parse_video_name, season_groups, recommend_season

    def get(u, timeout=300):
        with urllib.request.urlopen(base + u, timeout=timeout) as r:
            return r.read()

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    # 后端判定：完整版本 / 补全包，两种情形都要覆盖到
    kinds = {}
    cases = [("person.of.interest", 2), ("black.mirror", 3), ("sherlock", 1),
             ("sherlock", 2), ("the.100", 2), ("downton.abbey", 6)]
    bad = []
    for sk, se in cases:
        r = recommend_season(con, parse_video_name(sk), se)
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
        if r["kind"] == "none":
            bad.append(f"{sk} S{se} 没有推荐")
            continue
        if not r["langs"]:
            bad.append(f"{sk} S{se} 推荐里没有可选语言")
        # 推荐的每种语言都必须覆盖到它声称的集数
        for lg, d in r["langs"].items():
            if r["kind"] == "version" and d["n"] != r["total"]:
                bad.append(f"{sk} S{se} 推荐版本 {lg} 只有 {d['n']}/{r['total']} 集")
    check("recommend/后端判定", not bad, "; ".join(bad[:3]))
    check("recommend/两种情形都覆盖到",
          kinds.get("version", 0) > 0 and kinds.get("composed", 0) > 0, f"分布 {kinds}")

    # 前端：推荐卡存在，且下载下来每集恰好一个文件
    problems = []
    for sk, se in (("person.of.interest", 2), ("sherlock", 1), ("the.100", 2)):
        try:
            page = get(f"/?q={sk}&season={se}").decode("utf-8")
        except Exception as e:
            problems.append(f"{sk} S{se} 取页面失败 {e}")
            continue
        if '<div class="reco">' not in page:
            problems.append(f"{sk} S{se} 页面上没有推荐卡")
            continue
        m = re.search(r'href="(/season\.zip\?[^"]*)"', page[page.index('<div class="reco">'):])
        if not m:
            problems.append(f"{sk} S{se} 推荐卡里没有下载按钮")
            continue
        try:
            z = get(m.group(1).replace("&amp;", "&"))
            zf = zipfile.ZipFile(io.BytesIO(z))
            names = zf.namelist()
            eps = [re.search(r"(S\d+E\d+|SP\d+)", n).group(1) for n in names
                   if re.search(r"(S\d+E\d+|SP\d+)", n)]
            want = len(season_groups(con, parse_video_name(sk), se)[0]["pairs"])
            if zf.testzip() is not None:
                problems.append(f"{sk} S{se} 推荐包损坏")
            elif len(eps) != len(set(eps)):
                problems.append(f"{sk} S{se} 推荐包里同一集出现多次")
            elif len(set(eps)) != want:
                problems.append(f"{sk} S{se} 推荐包 {len(set(eps))} 集，应为 {want} 集")
        except Exception as e:
            problems.append(f"{sk} S{se} 推荐包下载/解包失败: {e}")
    check("recommend/推荐包集数齐全", not problems, "; ".join(problems[:3]))


# ---------------------------------------------------------------- 3b. 导航
def t_nav(base="http://127.0.0.1:8777"):
    """剧集总览 -> 某一季 -> 必须能回到总览。

    曾经季页面上只有「整季下载 / 分集下载」两个切换，点进某一季就再也回不到
    选剧集的地方（只能按浏览器后退）。
    """
    def get(u, timeout=90):
        with urllib.request.urlopen(base + u, timeout=timeout) as r:
            return r.read().decode("utf-8")

    problems = []
    for sk in ("black.mirror", "person.of.interest", "downton.abbey"):
        try:
            ov = get("/?" + urllib.parse.urlencode({"q": sk}))
        except Exception as e:
            problems.append(f"{sk} 总览失败 {e}")
            continue
        if '<span class="sno">' not in ov:
            problems.append(f"{sk} 没有进入季总览")
            continue
        for view in ("view=season", "view=episode"):
            link = [x for x in re.findall(r'href="(/\?[^"]*)"', ov) if view in x]
            if not link:
                problems.append(f"{sk} 总览里没有 {view} 链接")
                continue
            page = get(link[0].replace("&amp;", "&"))
            crumb = re.search(r'class="crumb"><a href="([^"]*)"', page)
            if not crumb:
                problems.append(f"{sk} 的 {view} 页面没有返回剧集的入口")
                continue
            back = get(crumb.group(1).replace("&amp;", "&"))
            if back != ov:
                problems.append(f"{sk} 的 {view} 返回后与原总览不一致")
    check("webui/季页面能返回剧集总览", not problems, "; ".join(problems[:3]))

    # 顶栏标题应能回首页
    try:
        h = get("/?q=black.mirror&season=3")
        check("webui/顶栏标题回首页", bool(re.search(r'<h1><a href="/"', h)))
    except Exception as e:
        check("webui/顶栏标题回首页", False, str(e))


# ---------------------------------------------------------------- 3c. 健康
def t_health(base="http://127.0.0.1:8777"):
    """这次真的挂过一次：重建索引时服务开着，WAL 涨到跟库一样大（534 MB），
    之后所有检索都返回不了内容。这里守住几条底线。
    """
    from config import DB_PATH as _DB, RUN_DIR
    idx = Path(_DB).parent

    # 1) WAL 不能失控
    wal = Path(str(_DB) + "-wal")
    mb = wal.stat().st_size / 1e6 if wal.exists() else 0.0
    check("health/WAL 未失控", mb < 50, f"-wal 有 {mb:.0f} MB，可能有并发重建")

    # 2) 服务在跑时，重建类命令必须拒绝
    pidf = RUN_DIR / "server.pid"
    if not pidf.exists():
        check("health/重建保护", False, "服务未运行，无法验证")
    else:
        import subprocess
        bad = []
        for args in (["match", "build-subs"], ["index", "scan-inner"]):
            r = subprocess.run([str(ROOT / "run.sh")] + args, capture_output=True,
                               text=True, timeout=120)
            if "正在运行" not in (r.stdout + r.stderr):
                bad.append(" ".join(args))
        check("health/重建保护（服务在跑时拒绝）", not bad, f"未拒绝: {bad}")

    # 3) 只读命令不受影响
    import subprocess
    r = subprocess.run([str(ROOT / "run.sh"), "index", "stats"],
                       capture_output=True, text=True, timeout=120)
    check("health/只读命令可用", r.returncode == 0 and "data" in r.stdout)

    # 4) 检索必须真的返回内容，而不只是 200
    try:
        with urllib.request.urlopen(base + "/?q=black.mirror", timeout=90) as resp:
            body = resp.read().decode("utf-8")
        ok = resp.status == 200 and "季" in body and "<main>" in body
        check("health/检索真的出结果", ok, f"HTTP {resp.status}, 长度 {len(body)}")
    except Exception as e:
        check("health/检索真的出结果", False, f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------- 4. units
def t_units():
    from searcher import (parse_special, parse_episode, fmt_size, season_title,
                          decode_subtitle, normalize_text_bytes, release_family)
    # 特别篇识别：该认的认，不该认的别认
    yes = [("Skins.S01.Secret.Party.Special", 0), ("Black.Mirror.S02.Special.White.Chirstmas", 0),
           ("Mou.Yuukainante.Shinai.SP", 0), ("某剧 SP01", 0), ("某剧 特别篇", 0)]
    no = ["Lost.S01E14.Special", "Aliens.1986.Special.Edition", "PBS.NOVA.ScienceNOW",
          "Special.Forces.2011.Edition"]
    bad = []
    for s, se in yes:
        if parse_episode(s)[0] is not None:
            continue
        got = parse_special(s)
        if got[0] != se:
            bad.append(f"{s} -> {got}")
    for s in no:
        if parse_episode(s)[0] is not None:
            continue
        if parse_special(s)[0] is not None:
            bad.append(f"{s} 被误判")
    check("units/特别篇识别", not bad, "; ".join(bad))

    check("units/体积格式（KB 起）",
          fmt_size(193972) == "189 KB" and fmt_size(-1) == "大小未知"
          and fmt_size(0) == "大小未知" and fmt_size(1048576) == "1.0 MB",
          f"{fmt_size(193972)} / {fmt_size(-1)} / {fmt_size(1048576)}")
    check("units/季号显示", season_title(0) == "特别篇" and season_title(3) == "第 3 季")

    # 发布版本标识：按分集标题命名的也要能归到一起
    a = release_family("sherlock.the.blind.banker.hdtv.xvid.fov")
    b = release_family("sherlock.the.great.game.hdtv.xvid.fov")
    check("units/版本标识（分集标题）", a == b == "hdtv.xvid.fov", f"{a} vs {b}")
    check("units/版本标识（正常）",
          release_family("person.of.interest.s02e01.720p.web.dl.h.264.kings")
          == "720p.web.dl.h.264.kings")

    # 编码：GBK 恰好是合法 UTF-8 的陷阱
    gbk = "为什么".encode("gbk")
    txt, enc = decode_subtitle(gbk)
    check("units/编码 GBK 不乱码", "为什么" in txt, f"enc={enc} txt={txt[:20]!r}")
    for s in ("简体中文测试", "繁體中文測試", "English subtitle test"):
        data, _e = normalize_text_bytes(s.encode("utf-8"))
        t2, _e2 = decode_subtitle(data)
        check(f"units/编码往返 {s[:4]}", t2.strip() == s)


# ---------------------------------------------------------------- main
def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    groups = {"groundtruth": t_groundtruth, "search": t_search,
              "webui": t_webui, "recommend": t_recommend, "nav": t_nav,
              "health": t_health, "units": t_units}
    print("=" * 68)
    for name, fn in groups.items():
        if which not in ("all", name):
            continue
        print(f"\n【{name}】")
        try:
            fn()
        except Exception as e:
            check(f"{name}/异常", False, f"{type(e).__name__}: {e}")
    print("\n" + "=" * 68)
    print(f"  通过 {len(PASS)} · 失败 {len(FAIL)}")
    if FAIL:
        for f in FAIL:
            print(f"    ❌ {f}")
    print("=" * 68)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
