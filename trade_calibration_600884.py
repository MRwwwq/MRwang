"""
600884杉杉股份 — PostgreSQL校准录入管道
修复点:
  1. get_db_conn缩进修正
  2. ticker从STOCK_CODE→600884
  3. solid_tech_tag从全固态→半固态电池(杉杉实际适配技术路径)
  4. qclaw_rule_id仅保留QClaw_Rule_021(固态题材专项)
  5. 适配pgpass+sslmode的PG17连接
"""
import psycopg2
import sys, os

def get_db_conn():
    """从pgpass读取密码,适配PG17 scram-sha-256"""
    # 读pgpass获取密码
    pgpass_path = os.path.expanduser("~/.pgpass")
    with open(pgpass_path) as f:
        pwd = f.read().strip().split(":")[-1]
    return psycopg2.connect(
        dbname="stock_data",
        user="stock_user",
        password=pwd,
        host="127.0.0.1",
        port="5432",
        sslmode="require"
    )

# 1. 前置强校验函数
def data_validate(row: dict) -> tuple[bool, str]:
    dt = row["trade_date"]
    if len(dt) != 8 or not dt.isdigit():
        return False, "日期格式错误，必须8位数字YYYYMMDD"
    pct = row["real_change_pct"]
    price = row["close_price"]
    if not isinstance(pct, (int, float)) or not -20 <= pct <= 20:
        return False, f"涨跌幅{pct}超出合法区间-20~20"
    if not isinstance(price, (int, float)) or price <= 0:
        return False, f"收盘价{price}非法，必须大于0"
    allow_action = {"持仓", "止盈", "止损", "空仓"}
    if row["real_trade_action"] not in allow_action:
        return False, f"操作仅允许{allow_action}"
    sp_desc = row["support_resistance_status"].strip()
    if len(sp_desc) < 4:
        return False, "支撑压力描述需写明突破/受阻/跌破/企稳，内容不可过短"
    if row["ai_pred"] not in ["预判上涨", "预判下跌", "预判中性"]:
        return False, "AI预判仅允许：预判上涨/预判下跌/预判中性"
    if row["ai_risk_tip"] not in ["提示入场", "提示减仓", "持有不动"]:
        return False, "风控提示仅允许：提示入场/提示减仓/持有不动"
    if "misjudge_hit_count" in row and (not isinstance(row["misjudge_hit_count"], int) or row["misjudge_hit_count"] < 0):
        return False, "芒格误判命中数量必须为非负整数"
    if "qclaw_rule_id" in row and len(row["qclaw_rule_id"].strip()) == 0:
        return False, "QClaw触发规则ID不可为空，无触发填写None"
    if "solid_tech_tag" in row and row["solid_tech_tag"] not in ["无固态题材", "半固态电池", "全固态电池", "双固态题材共振"]:
        return False, "固态题材标签仅允许：无固态题材/半固态电池/全固态电池/双固态题材共振"
    if "rag_match_score" in row and (not 0 <= float(row["rag_match_score"]) <= 1):
        return False, "RAG向量匹配度必须在0~1区间"
    return True, "数据校验通过"

# 2. 误差标签匹配
def get_error_label(ai_pred: str, real_change_pct: float, ai_risk_tip: str, is_trapped: bool, misjudge_hit_count:int=0, solid_tech_tag:str="无固态题材") -> str:
    if ai_pred == "预判上涨" and real_change_pct <= -2:
        return "【预判高估，负误差】"
    if ai_pred == "预判下跌" and real_change_pct >= 2:
        return "【预判低估，负误差】"
    if ai_risk_tip == "提示减仓" and real_change_pct <= -3:
        if misjudge_hit_count >= 4 or solid_tech_tag != "无固态题材":
            return "【风控判断有效-多重风险共振强化】"
        return "【风控判断有效】"
    if ai_risk_tip == "提示入场" and is_trapped:
        return "【入场条件失效】"
    return "【预判匹配，无误差】"

# 3. 入库
def insert_calibration(row: dict, error_tag: str):
    conn = get_db_conn()
    cur = conn.cursor()
    sql = """
    INSERT INTO trade_calibration(
        trade_date,ticker,real_change_pct,close_price,
        support_resistance_status,real_trade_action,
        ai_pred,ai_risk_tip,error_label,is_trapped,operator,update_time,
        misjudge_hit_count,solid_tech_tag,qclaw_rule_id,rag_match_score
    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),%s,%s,%s,%s)
    ON CONFLICT (trade_date,ticker) DO UPDATE SET
        real_change_pct=%s,close_price=%s,support_resistance_status=%s,
        real_trade_action=%s,ai_pred=%s,ai_risk_tip=%s,error_label=%s,
        is_trapped=%s,operator=%s,misjudge_hit_count=%s,solid_tech_tag=%s,
        qclaw_rule_id=%s,rag_match_score=%s,update_time=now();
    """
    params = (
        row["trade_date"], row["ticker"], row["real_change_pct"], row["close_price"],
        row["support_resistance_status"], row["real_trade_action"],
        row["ai_pred"], row["ai_risk_tip"], error_tag, row["is_trapped"], row["operator"],
        row["misjudge_hit_count"], row["solid_tech_tag"], row["qclaw_rule_id"], row["rag_match_score"],
        row["real_change_pct"], row["close_price"], row["support_resistance_status"],
        row["real_trade_action"], row["ai_pred"], row["ai_risk_tip"], error_tag,
        row["is_trapped"], row["operator"], row["misjudge_hit_count"], row["solid_tech_tag"],
        row["qclaw_rule_id"], row["rag_match_score"]
    )
    cur.execute(sql, params)
    conn.commit()
    cur.close()
    conn.close()
    print(f"✅ {row['ticker']} 校准数据入库完成")

# 4. 事后逻辑复核
def logic_review(trade_date: str, code_list: list):
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT ticker, real_change_pct, error_label, misjudge_hit_count, solid_tech_tag
        FROM trade_calibration
        WHERE trade_date=%s AND ticker IN %s
    """, (trade_date, tuple(code_list)))
    records = cur.fetchall()
    cur.close()
    conn.close()
    record_map = {r[0]:(r[1],r[2],r[3],r[4]) for r in records}
    missing = []
    logic_err = []
    for code in code_list:
        if code not in record_map:
            missing.append(code)
    for ticker, pct, tag, hit_num, solid_tag in record_map.items():
        if tag == "【预判高估，负误差】" and pct > -1:
            logic_err.append(f"{ticker}:标签为大跌负误差，实际跌幅不足1%，逻辑矛盾")
        if tag == "【预判低估，负误差】" and pct < 1:
            logic_err.append(f"{ticker}:标签为大涨负误差，实际涨幅不足1%，逻辑矛盾")
        if tag.startswith("【风控判断有效") and hit_num == 0 and solid_tag == "无固态题材":
            logic_err.append(f"{ticker}:标记风控有效，但无芒格误判、无固态题材，逻辑存疑")
    print("\n========== 盘后校准复核报告 ==========")
    if missing:
        print(f"\033[91m【漏录标的】{missing}\033[0m")
    if logic_err:
        print(f"\033[91m【逻辑冲突记录】{logic_err}\033[0m")
    if not missing and not logic_err:
        print("\033[92m全部数据完整、逻辑无异常，当日校准完成\033[0m")

if __name__ == "__main__":
    TODAY = "20260720"
    OPERATOR = "quant_bot"
    
    # 600884 杉杉股份实测数据(2026-07-17)
    daily_cal_data = [
        {
            "ticker": "600884",
            "real_change_pct": -2.73,
            "close_price": 11.77,
            "support_resistance_status": "持续跌破MA60支撑14.28，现价11.77距52周低点11.49仅2.4%，无企稳信号",
            "real_trade_action": "空仓",
            "ai_pred": "预判下跌",
            "ai_risk_tip": "提示减仓",
            "is_trapped": False,
            "operator": OPERATOR,
            "misjudge_hit_count": 5,
            "solid_tech_tag": "半固态电池",
            "qclaw_rule_id": "QClaw_Rule_021",
            "rag_match_score": 0.97
        }
    ]
    ALL_CODES = [i["ticker"] for i in daily_cal_data]
    print(f"===== 启动{TODAY}盘后校准录入（RAG/QClaw扩展字段同步） =====")
    for row in daily_cal_data:
        row["trade_date"] = TODAY
        pass_ok, msg = data_validate(row)
        if not pass_ok:
            print(f"\033[91m校验失败 {row['ticker']}:{msg}\033[0m")
            continue
        tag = get_error_label(
            row["ai_pred"], row["real_change_pct"], row["ai_risk_tip"], row["is_trapped"],
            row["misjudge_hit_count"], row["solid_tech_tag"]
        )
        print(f"  误差标签: {tag}")
        insert_calibration(row, tag)
    logic_review(TODAY, ALL_CODES)
    print("✅ 当日录入流程执行完毕")
