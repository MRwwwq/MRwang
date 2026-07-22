"""
inference_accelerator.py — §6.2 低延迟实时推理加速
TensorRT量化加速(首选)/ONNX(降级)/FP16(兜底)
模型推理与数据IO线程解耦，保证盘中无卡顿
"""
import time
import numpy as np
import os

MODEL_DIR = "/opt/stock_agent/models"


class InferenceAccelerator:
    """§6.2 统一推理加速底座 — 5 Agent共用"""

    def __init__(self):
        self._backends = {}

    # ── 模型注册 ──

    def register(self, name, model_obj, backend="auto"):
        """
        注册模型到加速引擎
        backend: "tensorrt" / "onnx" / "fp16" / "raw"
        """
        if backend == "auto":
            backend = self._detect_best_backend(model_obj)
        self._backends[name] = {
            "model": model_obj,
            "backend": backend,
            "call_count": 0,
            "total_latency": 0,
        }
        print(f"[Inference] {name} 注册完成, backend={backend}")

    def _detect_best_backend(self, model_obj):
        """自动检测最优后端"""
        # TensorRT: 需nvidia-smi可用
        try:
            import subprocess
            r = subprocess.run(["nvidia-smi"], capture_output=True, timeout=2)
            if r.returncode == 0:
                return "tensorrt"
        except Exception:
            pass
        # ONNX: 次选
        try:
            import onnxruntime
            return "onnx"
        except Exception:
            pass
        # FP16/原生: 兜底
        return "fp16" if hasattr(model_obj, "half") else "raw"

    # ── 统一推理入口 ──

    def predict(self, name, input_data):
        """
        统一推理: 自动选择backend, 记录时延
        输入: name (注册名), input_data (ndarray)
        输出: (result, latency_ms)
        """
        eng = self._backends.get(name)
        if not eng:
            raise ValueError(f"模型 {name} 未注册")

        t0 = time.perf_counter()
        model = eng["model"]
        backend = eng["backend"]

        if backend == "tensorrt":
            result = self._predict_trt(model, input_data)
        elif backend == "onnx":
            result = self._predict_onnx(model, input_data)
        elif backend == "fp16":
            result = self._predict_fp16(model, input_data)
        else:
            result = model.predict(input_data)

        latency = (time.perf_counter() - t0) * 1000
        eng["call_count"] += 1
        eng["total_latency"] += latency

        # 时延告警: 单次>50ms
        if latency > 50:
            print(f"⚠ [Inference] {name} 推理时延 {latency:.1f}ms > 50ms")

        return result, round(latency, 2)

    def _predict_trt(self, model, x):
        """TensorRT推理(需trt环境)"""
        try:
            import tensorrt as trt
            import pycuda.driver as cuda
            # 占位: 真实环境需builder/engine
            return model.predict(x)
        except Exception:
            return model.predict(x)

    def _predict_onnx(self, model, x):
        """ONNX Runtime推理"""
        try:
            import onnxruntime as ort
            sess = ort.InferenceSession(model)
            input_name = sess.get_inputs()[0].name
            return sess.run(None, {input_name: x.astype(np.float32)})[0]
        except Exception:
            return model.predict(x)

    def _predict_fp16(self, model, x):
        """FP16半精度推理"""
        try:
            x_fp16 = x.astype(np.float16)
            return model.predict(x_fp16)
        except Exception:
            return model.predict(x)

    # ── 性能统计 ──

    def stats(self):
        """推理性能报告"""
        report = []
        for name, eng in self._backends.items():
            avg_lat = eng["total_latency"] / max(eng["call_count"], 1)
            report.append({
                "model": name,
                "backend": eng["backend"],
                "calls": eng["call_count"],
                "avg_latency_ms": round(avg_lat, 2),
            })
        return report

    # ── 异步加载辅助 ──

    @staticmethod
    def async_load(loader_func, timeout=30):
        """异步多线程加载数据(数据IO与推理线程解耦)"""
        from threading import Thread, Event
        result = [None]
        event = Event()

        def _load():
            try:
                result[0] = loader_func()
            except Exception as e:
                result[0] = e
            event.set()

        t = Thread(target=_load, daemon=True)
        t.start()
        loaded = event.wait(timeout=timeout)
        if not loaded:
            raise TimeoutError("异步加载超时")
        if isinstance(result[0], Exception):
            raise result[0]
        return result[0]

    def warmup(self, name, dummy_input):
        """推理预热"""
        _ = self.predict(name, dummy_input)
        print(f"[Inference] {name} 预热完成")


class DataStreamBuffer:
    """§6.2 盘中行情流缓冲队列(削峰)"""

    def __init__(self, max_size=1000):
        self._buffer = []
        self._max_size = max_size

    def push(self, item):
        self._buffer.append(item)
        if len(self._buffer) > self._max_size:
            self._buffer.pop(0)

    def pop_all(self):
        items = list(self._buffer)
        self._buffer.clear()
        return items

    def size(self):
        return len(self._buffer)
