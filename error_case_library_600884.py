"""
error_case_library.py — 研判漏洞人工修正入库模块
必须在当日13模块完整报告生成后执行
"""
import psycopg2, os, sys, json
from datetime import datetime

sys.path.insert(0, '/opt/stock_agent')

DB_PARAMS = dict(host="127.0.0.1", port=5432, dbname="stock_data", user="stock_user", sslmode="require")
def _get_conn():
    with open(os.path.expanduser("~/.pgpass")) as f:
        DB_PARAMS["password"] = f.read().strip().split(":")[-1]
    return psycopg2.connect(**DB_PARAMS)

VALID_CATEGORIES = {
    "资金维度盲区", "估值维度盲区", "筹码测算盲区",
    "技术面盲区", "固态题材盲区", "芒格误判盲区", "QClaw规则盲区"
}

CATEGORY_WEIGHT_MAP = {
    "资金维度盲区": "资金因子权重下调",
    "估值维度盲区": "PE系数上调(1.3->1.5)",
    "筹码测算盲区": "筹码压力系数上调",
    "技术面盲区": "技术评分权重下调",
    "固态题材盲区": "Rule021权重上调",
    "芒格误判盲区": "misjudge_threshold下调(3->2)",
    "QClaw规则盲区": "Rule021阈值调整"
}

def init_table():
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS error_case_library (
            id SERIAL PRIMARY KEY,
            ticker VARCHAR(10) NOT NULL,
            trade_date VARCHAR(8) NOT NULL,
            defect_category VARCHAR(20) NOT NULL,
            defect_description TEXT NOT NULL,
            market_evidence TEXT NOT NULL,
            optimization_suggestion TEXT NOT NULL,
            operator VARCHAR(20) NOT NULL,
            created_at TIMESTAMP DEFAULT NOW(),
            iteration_status VARCHAR(10) DEFAULT 'pending',
            UNIQUE(ticker, trade_date, defect_category)
        )
    """)
    conn.commit()
    cur.close(); conn.close()
    print("init error_case_library OK")

def insert_case(case: dict):
    cat = case["defect_category"]
    if cat not in VALID_CATEGORIES:
        raise ValueError(f"invalid category {cat}, must be one of {VALID_CATEGORIES}")
    if len(case["defect_description"].strip()) < 10:
        raise ValueError("defect_description too short, min 10 chars")
    if len(case["market_evidence"].strip()) < 10:
        raise ValueError("market_evidence too short, min 10 chars")

    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO error_case_library
            (ticker, trade_date, defect_category, defect_description,
             market_evidence, optimization_suggestion, operator)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (ticker, trade_date, defect_category) DO UPDATE SET
            defect_description=EXCLUDED.defect_description,
            market_evidence=EXCLUDED.market_evidence,
            optimization_suggestion=EXCLUDED.optimization_suggestion,
            operator=EXCLUDED.operator,
            created_at=NOW(),
            iteration_status='pending'
    """, (
        case["ticker"], case["trade_date"], case["defect_category"],
        case["defect_description"], case["market_evidence"],
        case["optimization_suggestion"], case["operator"]
    ))
    conn.commit()
    cur.close(); conn.close()
    print(f"[{case['defect_category']}] {case['ticker']}/{case['trade_date']} OK, weight_impact={CATEGORY_WEIGHT_MAP.get(cat, 'N/A')}")

def query_pending():
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, ticker, trade_date, defect_category,
               LEFT(defect_description, 60) as desc_short,
               operator, created_at::text
        FROM error_case_library
        WHERE iteration_status = 'pending'
        ORDER BY created_at DESC
    """)
    rows = cur.fetchall()
    cur.close(); conn.close()
    return rows

def pre_iteration_check():
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM error_case_library WHERE iteration_status = 'pending'")
    cnt = cur.fetchone()[0]
    cur.close(); conn.close()
    if cnt == 0:
        print("NO pending error cases, blocking iteration")
        print("error_case_library must have >=1 pending record before monthly tuning")
        sys.exit(1)
    print(f"error_case_library: {cnt} pending records, iteration allowed")
    return cnt

def combined_pre_check(ticker, week_dates):
    """Module1(calibration) + Module2(error_case) 双轨校验"""
    print("=== Dual-track pre-iteration check ===")
    # Module1: trade calibration完整性
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(DISTINCT to_char(trade_date,'YYYYMMDD'))
        FROM trade_calibration
        WHERE ticker = %s AND to_char(trade_date,'YYYYMMDD') = ANY(%s)
    """, (ticker, week_dates))
    cal_cnt = cur.fetchone()[0]
    cur.close(); conn.close()
    
    if cal_cnt < len(week_dates):
        print(f"[MODULE1] trade_calibration缺失: 应有{len(week_dates)}日, 实有{cal_cnt}日 -> BLOCK")
        sys.exit(1)
    print(f"[MODULE1] trade_calibration完整: {cal_cnt}/{len(week_dates)}日 OK")
    
    # Module2: error_case_library有记录
    ec_cnt = pre_iteration_check()
    print(f"[MODULE2] error_case_library: {ec_cnt}条待修复 OK")
    print("Dual-track check PASSED, iteration allowed")

if __name__ == "__main__":
    init_table()
    TODAY = "20260720"
    OPERATOR = "quant_reviewer"
    
    # 约束: 必须在当日13模块完整报告生成后执行
    report_path = "/opt/stock_agent/reports/report_600884_20260720_v4.md"
    if not os.path.exists(report_path):
        print(f"Report not found: {report_path}, aborting")
        sys.exit(1)
    print(f"13-module report confirmed: {report_path}")
    
    # 漏洞1: 资金维度盲区 - 北向背离未被充分量化
    insert_case({
        "ticker": "600884", "trade_date": TODAY,
        "defect_category": "资金维度盲区",
        "defect_description": "北向Q2+123%和国内主力15日-9.77亿背离幅度达2.3倍标准差,但未建立偏离度量化指标.应生成统一的'资金分歧系数'纳入综合评分而非仅标注背离信号.",
        "market_evidence": "07/20收盘11.32,60日跌幅-22.26%.北向Q2持仓4372万股(2.38%),国内主力15日-9.77亿.两股方向完全相反幅度差达近年极值.",
        "optimization_suggestion": "新增北向vs国内主力偏离度因子:偏离>1.5sigma扣10分,>2.5sigma扣20分,替代当前纯文字标注.",
        "operator": OPERATOR
    })
    
    # 漏洞2: 固态题材盲区 - 全固态利空权重不足
    insert_case({
        "ticker": "600884", "trade_date": TODAY,
        "defect_category": "固态题材盲区",
        "defect_description": "全固态电池2030+锂金属替代石墨风险仅标注长期威胁,杉杉核心负极(营收~35%)估值折价未纳入综合评分,导致赛道得分35偏高.应直接施加负极业务长期折价系数.",
        "market_evidence": "杉杉无锂金属负极布局(营收0%).行业预测2030全固态渗透率>10%,对应杉杉负极需求损失5-8亿营收.",
        "optimization_suggestion": "对负极利润部分施加折价系数0.9,引入技术颠覆距离指标:距商业化越近折价系数从0.9线性降至0.5.",
        "operator": OPERATOR
    })
    
    # 漏洞3: 技术面盲区 - MA60破位后筹码结构未量化
    insert_case({
        "ticker": "600884", "trade_date": TODAY,
        "defect_category": "技术面盲区",
        "defect_description": "MA60(14.28)持续跌破但未量化60日筹码成本(13.90)与现价(11.32)套牢深度及解套抛压.仅靠均线排列判定空头,缺少筹码成本分布压力量化.",
        "market_evidence": "07/20收盘11.32,成本13.90.按日均成交额6亿解套所有套牢盘约需45交易日,构成中长期压制.",
        "optimization_suggestion": "新增筹码套牢深度=(均线成本-现价)/现价*100%.深度>15%直接归零技术评分.增加预估解套所需交易日.",
        "operator": OPERATOR
    })
    
    # 漏洞4: 芒格误判盲区 - 因子退潮速率被遗漏
    insert_case({
        "ticker": "600884", "trade_date": TODAY,
        "defect_category": "芒格误判盲区",
        "defect_description": "07/20比07/17:因子02(77.2->39.4),15(87->36),23(80->31)三次同时大幅下降但Lollapalooza仅计数量未分析退潮速率,遗漏情绪结构变化研判价值.",
        "market_evidence": "07/17:8项高危/平均38.98分;07/20:5项高危/平均27.9分.3项降幅>40分但Lolla状态从8变5仅改数量未分析修复.",
        "optimization_suggestion": "连续2日高危因子数量降幅>=3项时触发Lolla降级观察模式:一票否决保留但仓位上限从0%放宽至3%.添加因子退潮速率监测.",
        "operator": OPERATOR
    })
    
    print(f"600884 error cases inserted: 4 records")
    pending = query_pending()
    print(f"Pending cases: {len(pending)}")
    for p in pending:
        print(f"  #{p[0]} {p[1]}/{p[2]} [{p[3]}] {p[4]}...")
    
    # 演示双轨校验(本周数据不全,预期阻断)
    print("\n=== Dual-track check demo ===")
    week = ["20260720","20260719","20260718","20260717","20260716"]
    try:
        combined_pre_check("600884", week)
    except SystemExit:
        print("(expected block: week data incomplete)")
