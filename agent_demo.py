# agent_demo.py
import numpy as np
from persistent_memory import PersistentMemory

# 1. 智能体启动第一步：加载全部持久记忆
memory = PersistentMemory()


def agent_decision(current_stock_info: dict, model_raw_score: float):
    """
    单次选股决策，强制读取记忆，无记忆校验不输出交易信号
    """
    stock_code = current_stock_info["stock_code"]
    current_feat = np.array(current_stock_info["feature"], dtype=np.float32)

    # 步骤1：全局黑名单记忆拦截
    if memory.is_in_blacklist(stock_code):
        print(f"【记忆风控拦截】{stock_code} 历史大额亏损，永久黑名单，跳过开仓")
        return None

    # 步骤2：强制检索相似历史交易记忆
    similar_history = memory.get_similar_history(current_feat)

    if not similar_history:
        print(f"【记忆检索提示】{stock_code} 无相似历史，使用原始模型分")
        adjust_score = model_raw_score
    else:
        # 步骤3：使用历史记忆修正模型原始得分
        adjust_score = memory.adjust_score_by_history(model_raw_score, similar_history)
        print(f"原始预测分:{model_raw_score:.3f}，记忆修正后得分:{adjust_score:.3f}")
        print(f"  相似案例{len(similar_history)}条: 盈利{memory._last_adj_info['good_count']}条 / 亏损{memory._last_adj_info['bad_count']}条")

    # 步骤4：输出最终交易动作
    action = {
        "stock_code": stock_code,
        "final_score": adjust_score,
        "similar_case_count": len(similar_history),
        "trade_signal": "buy" if adjust_score > 0.6 else "hold"
    }
    return action


def after_close_save_memory(finish_trade: dict):
    """平仓完成自动归档永久记忆"""
    memory.save_trade_memory(finish_trade)
    print("交易经验已永久存入记忆库，重启不丢失")


# ==================== 模拟运行测试 ====================

if __name__ == "__main__":
    # 模拟当前个股特征
    test_stock = {
        "stock_code": "600XXX",
        "market_env": "震荡",
        "industry": "半导体",
        "feature": [0.12, 0.45, 0.77, 0.22, 0.56, 0.89, 0.31, 0.66, 0.19, 0.73]
    }
    # AI模型原始预测分数
    raw_score = 0.62

    # 执行带记忆的决策
    res = agent_decision(test_stock, raw_score)
    print("决策结果：", res)

    # 模拟平仓，存入永久记忆（亏损案例，自动拉黑标的）
    finish_data = {
        "stock_code": "600XXX",
        "market_env": "震荡",
        "industry": "半导体",
        "feature": [0.12, 0.45, 0.77, 0.22, 0.56, 0.89, 0.31, 0.66, 0.19, 0.73],
        "open_price": 12.5,
        "close_price": 11.8,
        "profit_rate": -0.056,
        "hold_days": 3
    }
    after_close_save_memory(finish_data)

    # 再次选股会被黑名单拦截
    res2 = agent_decision(test_stock, 0.7)
    print("二次决策结果：", res2)

    # 程序退出释放资源
    memory.close_all()
