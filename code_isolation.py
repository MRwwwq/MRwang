"""
code_isolation.py — §6.5 代码权限隔离安全机制
分层权限: 核心固化模块(只读) / 可调参数(受限) / 新因子(开放)
所有AI自主修改内容自动版本快照, 一键回滚
"""
import os
import json
import shutil
import hashlib
from datetime import datetime

SNAPSHOT_DIR = "/opt/stock_agent/snapshots"
MANIFEST_PATH = "/opt/stock_agent/snapshot_manifest.json"

# §6.5 只读核心模块(不可被AI迭代覆盖)
READONLY_MODULES = [
    "layered_risk_control.py",
    "static_hard_risk_control.py",
    "full_integrated_pipeline.py",
    "agent_orchestrator.py",
    "agent_risk_controller.py",
    "chain_logger.py",
    "code_isolation.py",
]

# §6.5 可调参数模块(仅允许微调阈值/权重)
TUNABLE_MODULES = [
    "agent_predict_v2.py",      # 打分阈值
    "agent_position.py",        # 仓位系数
    "dynamic_ai_risk.py",       # 动态风控参数
    "agent_selector.py",        # 选股权重
]

# §6.5 开放模块(可新增补充)
OPEN_MODULES = [
    "factor_weekly_iterate.py",   # 新因子
    "evolution_engine.py",        # 参数进化
    "agent_evolver.py",           # 进化调度
]


class CodeIsolation:
    """§6.5 代码隔离 — 分层权限 + 版本快照 + 一键回滚"""

    def __init__(self, base_dir="/opt/stock_agent"):
        self.base_dir = base_dir
        os.makedirs(SNAPSHOT_DIR, exist_ok=True)
        self.manifest = self._load_manifest()

    def _load_manifest(self):
        try:
            with open(MANIFEST_PATH) as f:
                return json.load(f)
        except Exception:
            return {"versions": [], "baseline_hash": {}}

    def _save_manifest(self):
        with open(MANIFEST_PATH, "w") as f:
            json.dump(self.manifest, f, ensure_ascii=False, indent=2)

    def _file_hash(self, path):
        """计算文件SHA256"""
        try:
            with open(path, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()[:16]
        except Exception:
            return ""

    # ── 基线锁定 ──

    def lock_baseline(self):
        """
        记录当前所有核心模块的原始哈希 == 只读基线
        进化Agent修改后自动对比, 篡改核心模块则淘汰
        """
        baseline = {}
        for fname in READONLY_MODULES + TUNABLE_MODULES + OPEN_MODULES:
            fpath = os.path.join(self.base_dir, fname)
            if os.path.exists(fpath):
                baseline[fname] = self._file_hash(fpath)
        self.manifest["baseline_hash"] = baseline
        self._save_manifest()
        print(f"[CodeIsolation] 基线锁定: {len(baseline)}文件")

    # ── 分层权限校验 ──

    def check_modify_permission(self, file_name):
        """
        检查文件是否可被AI修改
        返回: (allow, reason)
        """
        if file_name in READONLY_MODULES:
            return False, f"§6.5 ❌ {file_name} 为只读核心模块, AI无权修改"
        if file_name in TUNABLE_MODULES:
            return True, f"§6.5 ⚠ {file_name} 为可调模块, 允许微调参数阈值"
        if file_name in OPEN_MODULES:
            return True, f"§6.5 ✅ {file_name} 为开放模块, 允许新增补充"
        # 不在列表中默认不可修改
        return False, f"§6.5 ❌ {file_name} 未注册, 禁止修改"

    def validate_evolution(self, changed_files):
        """
        沙盒阶段: 校验进化是否篡改核心固化规则
        changed_files: [(fname, before_hash, after_hash), ...]
        返回: (pass, [违规项])
        """
        violations = []
        for fname, before, after in changed_files:
            baseline = self.manifest.get("baseline_hash", {}).get(fname)
            if fname in READONLY_MODULES and baseline:
                if before != baseline or after != baseline:
                    violations.append(f"{fname} 核心模块被篡改")
            if fname in TUNABLE_MODULES and baseline:
                if before != baseline:
                    pass  # 允许微調
        if violations:
            print(f"[CodeIsolation] ❌ 进化违规: {'; '.join(violations)}")
        else:
            print("[CodeIsolation] ✅ 进化合规, 未篡改核心模块")
        return len(violations) == 0, violations

    # ── 版本快照 ──

    def snapshot(self, version_tag=None):
        """
        创建当前全量代码快照
        返回: snapshot_id
        """
        if version_tag is None:
            version_tag = datetime.now().strftime("%Y%m%d_%H%M%S")
        snapshot_dir = os.path.join(SNAPSHOT_DIR, f"v{version_tag}")
        os.makedirs(snapshot_dir, exist_ok=True)

        files = READONLY_MODULES + TUNABLE_MODULES + OPEN_MODULES
        for fname in files:
            src = os.path.join(self.base_dir, fname)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(snapshot_dir, fname))

        # 记录manifest
        record = {
            "version": version_tag,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "files": len(files),
            "path": snapshot_dir,
        }
        self.manifest["versions"].append(record)
        self._save_manifest()
        print(f"[CodeIsolation] 📸 快照 v{version_tag}: {len(files)}文件")
        return version_tag

    # ── 一键回滚 ──

    def rollback(self, version_tag):
        """回滚到指定版本快照"""
        target = os.path.join(SNAPSHOT_DIR, f"v{version_tag}")
        if not os.path.isdir(target):
            return False, f"版本 v{version_tag} 不存在"

        files = READONLY_MODULES + TUNABLE_MODULES + OPEN_MODULES
        restored = 0
        for fname in files:
            src = os.path.join(target, fname)
            dst = os.path.join(self.base_dir, fname)
            if os.path.exists(src):
                shutil.copy2(src, dst)
                restored += 1

        print(f"[CodeIsolation] ↩️ 回滚至 v{version_tag}: {restored}文件恢复")
        return True, f"回滚成功: {restored}文件"

    def list_versions(self):
        """列出所有快照版本"""
        return list(reversed(self.manifest.get("versions", [])))

    # ── 沙盒准入 ──

    def sandbox_admission_check(self, changed_files):
        """沙盒阶段完整准入检查"""
        allow, violations = self.validate_evolution(changed_files)
        if not allow:
            print("[CodeIsolation] ❌ 沙盒准入拒绝: 违规修改核心模块")
        else:
            print("[CodeIsolation] ✅ 沙盒准入通过")
        return allow, violations
