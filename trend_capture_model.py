import torch
import torch.nn as nn
import pandas as pd
import numpy as np
from torch.utils.data import Dataset, DataLoader


# ===================== 时序数据集 =====================

class StockSeqDataset(Dataset):
    """60日滑动窗口时序数据集"""
    def __init__(self, seq_df, seq_len=60):
        self.seq_len = seq_len
        self.feat_cols = [
            "close", "volume", "macd", "rsi", "ma5", "ma20", "ma60",
            "sentiment_score", "guba_hot", "base_factor_score"
        ]
        self.data = seq_df[self.feat_cols].values.astype(np.float32)
        self.label = seq_df["future_10d_return"].values.astype(np.float32)

    def __len__(self):
        return len(self.data) - self.seq_len

    def __getitem__(self, idx):
        seq_x = self.data[idx:idx + self.seq_len]
        y = self.label[idx + self.seq_len]
        return torch.from_numpy(seq_x), torch.tensor(y)


# ===================== 多头自注意力Transformer块 =====================

class MultiHeadAttentionBlock(nn.Module):
    def __init__(self, dim, head_num=4):
        super().__init__()
        self.attn = nn.MultiheadAttention(dim, head_num, batch_first=True)
        self.norm = nn.LayerNorm(dim)
        self.ffn = nn.Sequential(
            nn.Linear(dim, dim * 2), nn.GELU(), nn.Linear(dim * 2, dim)
        )

    def forward(self, x):
        attn_out, _ = self.attn(x, x, x)
        x = self.norm(x + attn_out)
        x = self.norm(x + self.ffn(x))
        return x


# ===================== LSTM+Transformer混合时序模型 =====================

class TimingTrendModel(nn.Module):
    """三层时序捕捉：短LSTM→中LSTM→长周期自注意力"""
    def __init__(self, feat_dim=10, hidden_dim=32, seq_len=60):
        super().__init__()
        self.seq_len = seq_len

        # 短周期LSTM（1-10日波动）
        self.short_lstm = nn.LSTM(feat_dim, hidden_dim, batch_first=True)
        # 中周期LSTM（10-30日趋势）
        self.mid_lstm = nn.LSTM(hidden_dim, hidden_dim, batch_first=True)
        # 长周期多头自注意力（30-60日周期+板块联动）
        self.long_attn = MultiHeadAttentionBlock(hidden_dim)
        # 输出预测头
        self.pred_head = nn.Sequential(
            nn.Linear(hidden_dim, 16), nn.ReLU(), nn.Linear(16, 1)
        )

    def forward(self, x):
        # x: [batch, seq_len, feat_dim]
        short_out, _ = self.short_lstm(x)
        mid_out, _ = self.mid_lstm(short_out)
        long_out = self.long_attn(mid_out)
        # 取序列最后时刻特征做收益预测
        final_feat = long_out[:, -1, :]
        pred = self.pred_head(final_feat)
        return pred.squeeze(-1)


# ===================== 时序模型训练、推理、板块联动分析封装 =====================

class TrendCaptureModel:
    def __init__(self, seq_len=60, device="cpu"):
        self.seq_len = seq_len
        self.device = torch.device(device)
        self.model = TimingTrendModel(seq_len=seq_len).to(self.device)
        self.feat_cols = [
            "close", "volume", "macd", "rsi", "ma5", "ma20", "ma60",
            "sentiment_score", "guba_hot", "base_factor_score"
        ]

    def train_model(self, train_df, valid_df, epoch=80, batch=64, lr=1e-4):
        train_ds = StockSeqDataset(train_df, self.seq_len)
        valid_ds = StockSeqDataset(valid_df, self.seq_len)
        train_loader = DataLoader(train_ds, batch_size=batch, shuffle=True)
        valid_loader = DataLoader(valid_ds, batch_size=batch, shuffle=False)

        loss_fn = nn.MSELoss()
        opt = torch.optim.Adam(self.model.parameters(), lr=lr)

        for e in range(epoch):
            self.model.train()
            train_loss = 0.0
            for seq_x, y in train_loader:
                seq_x, y = seq_x.to(self.device), y.to(self.device)
                pred = self.model(seq_x)
                loss = loss_fn(pred, y)
                opt.zero_grad()
                loss.backward()
                opt.step()
                train_loss += loss.item()

            self.model.eval()
            val_loss = 0.0
            with torch.no_grad():
                for seq_x, y in valid_loader:
                    seq_x, y = seq_x.to(self.device), y.to(self.device)
                    pred = self.model(seq_x)
                    val_loss += loss_fn(pred, y).item()

            if (e + 1) % 20 == 0:
                print("Epoch{:3d} TrainLoss:{:.6f} ValLoss:{:.6f}".format(
                    e + 1, train_loss / len(train_loader), val_loss / len(valid_loader)))

    def predict_trend_score(self, stock_seq_df: pd.DataFrame) -> float:
        """输入单只股票时序DataFrame(需含feat_cols), 输出0~1趋势分"""
        self.model.eval()
        seq_data = stock_seq_df[self.feat_cols].values[-self.seq_len:].astype(np.float32)
        seq_tensor = torch.from_numpy(seq_data).unsqueeze(0).to(self.device)
        with torch.no_grad():
            raw_pred = self.model(seq_tensor).cpu().item()
        # 归一化至0-1趋势打分
        trend_score = np.clip((raw_pred + 0.15) / 0.3, 0, 1)
        return round(trend_score, 4)

    def get_industry_correlation(self, industry_seq_dict: dict):
        """批量输入同板块个股时序dict{code: df}, 输出板块联动强度"""
        industry_trend = {}
        for code, df in industry_seq_dict.items():
            industry_trend[code] = self.predict_trend_score(df)
        trend_series = pd.Series(industry_trend)
        # 板块内部联动强度(标准差越小→联动越强)
        corr_score = trend_series.std()
        return {
            "industry_trend_map": industry_trend,
            "plate_link_strength": round(corr_score, 4),
            "mean_trend": round(trend_series.mean(), 4),
            "max_trend": round(trend_series.max(), 4),
            "min_trend": round(trend_series.min(), 4),
        }

    def save_weight(self, path="trend_model.pth"):
        torch.save(self.model.state_dict(), path)

    def load_weight(self, path="trend_model.pth"):
        self.model.load_state_dict(torch.load(path, map_location=self.device))
