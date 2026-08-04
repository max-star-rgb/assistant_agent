# Multimodal embedding system eval

该正式本地 system eval 验证同一套 SigLIP2 ONNX 资产的 image/text readiness、共同向量空间、
固定输入可重复性、正负样本排序，以及 CUDA-only execution provider 边界。

`--dry-run` 只报告配置和预期检查，不读取 manifest、不创建 ONNX session。真实执行必须由 operator
显式传入 `--allow-local-model`，结果写入 `.data/evals/system/multimodal_embedding/`；artifact
不包含向量、查询文本、图片内容或本地媒体路径。

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_system_multimodal_embedding_eval.py --dry-run

/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_system_multimodal_embedding_eval.py \
  --allow-local-model \
  --model-dir .local/models/siglip2-base-patch16-224
```
