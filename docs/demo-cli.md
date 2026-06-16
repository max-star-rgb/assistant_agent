# Demo CLI

The local assistant CLI is:

```bash
python scripts/run_assistant_cli.py
```

## Text Prompt

```bash
python scripts/run_assistant_cli.py --text "帮我写一段商品介绍"
python scripts/run_assistant_cli.py --text "生成一张日系极简海报"
```

## Mock Image Or Video References

```bash
python scripts/run_assistant_cli.py --text "看看这张图里有什么商品" --image-ref demo_image_product_1
python scripts/run_assistant_cli.py --text "总结这个视频里的商品和场景" --video-ref demo_video_product_1
```

These references are logical mock/local ids. They are not real file paths.

## Scenario Mode

```bash
python scripts/run_assistant_cli.py --scenario product_search_compare
python scripts/run_assistant_cli.py --scenario full_multistep_image_search_compare_generate
```

Scenario ids come from:

```text
demo_data/scenarios/e2e_demo_scenarios.json
```

## Output Formats

Default JSON:

```bash
python scripts/run_assistant_cli.py --text "帮我写一段商品介绍"
```

Readable text:

```bash
python scripts/run_assistant_cli.py --text "帮我写一段商品介绍" --format text
```

All default CLI commands remain offline and use mock/local providers.
