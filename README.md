# limi-models

ljm 和米鱼一起养的模型配置库。`limi` = ljm + miyu。

这里不放权重，只放"怎么把这个模型训出来、怎么用好它"的全部信息：训练脚本、数据构成、参数、触发词、推荐权重、已知缺陷、权重指纹。换台机器、隔几个月，照着这些能把同一个模型重建出来。

## 现在收录

| 模型 | 版本 | 定位 | 权重 |
| --- | --- | --- | --- |
| 嗷呜虎虎 | v1 | 只训脸的第一版 | `aowo_sd15_id_v0a.safetensors` 38MB |
| 嗷呜虎虎 | v2 | 脸/身体拆开训 | `aowo_sd15_char_v2` / `aowo_sd15_id_v0c` 38MB |
| 嗷呜虎虎 | v3 | 身体单训，640 | `aowo_sd15_body_v3.safetensors` 38MB |
| 嗷呜虎虎 | v3.1 | 脸身合并 | `aowo_sd15_char_v3_1.safetensors` 38MB |
| 嗷呜虎虎 | v3.2 | 换数据再训 | `aowo_sd15_char_v3_2.safetensors` 38MB |
| 嗷呜虎虎 | v3.3 | 脸倒退，弃用 | `aowo_sd15_char_v3_3.safetensors` 38MB |
| 嗷呜虎虎 | v3.4 | v3 线里唯一能用的 | `aowo_sd15_char_v3_4.safetensors` 38MB |
| 嗷呜虎虎 | v4 | 混合训练，768，双头开始出现 | `aowo_sd15_char_v4.safetensors` 27MB |
| 嗷呜虎虎 | v5 | 按比例重裁，衣服成型 | `aowo_sd15_char_v5.safetensors` 27MB |
| 嗷呜虎虎 | v5.1 | 稳，出错率最低 | `aowo_sd15_char_v5_1.safetensors` 54MB |
| 嗷呜虎虎 | v5.2 | 细节最强，脸被稀释 | `aowo_sd15_char_v5_2.safetensors` 82MB |
| 嗷呜虎虎 | v5.3 | 结构最松，弃用 | `aowo_sd15_char_v5_3.safetensors` 54MB |
| 嗷呜虎虎 | v5.4 | 画质最好，脸最立体（现主力） | `aowo_sd15_char_v5_4.safetensors` 54MB |
| 马吉 Machi | v1 | 第二个人，两套 cos 素材（714 张） | `machi_sd15_legacy_v1.safetensors` 52MB |
| 马吉 Machi | v2 | 原生分辨率重切（729 张），结构更稳 | `machi_sd15_legacy_v2.safetensors` 52MB |

两版权重都在 `~/ComfyUI/models/loras/`，不进本库。

## 一条时间线

- v5.0 起把训练素材从"768 缩放图"换成"4000px 原图按三种比例重新裁"（1:1 脸 / 3:4 胸像 / 2:3 全身）。**双头问题的根因就是训练集缺 3:4 比例**，补上之后消失。
- v5.2（dim 96 / 896）细节最强，但脸被稀释、露得多，降级为对照。
- v5.3（说明去掉五官词 + 脸图加权）结构最松，弃用。
- v5.4 = v5.1 的数据思路 + 74 张三种比例含翻转，dim 64 / 832 / 2600 步，目前画质最好。

## 目录规矩

```
limi-models/
  <模型名>/
    v<版本>.md          # 版本卡：参数、触发词、推荐权重、缺陷、sha256
    train_v<版本>.sh    # 该版实际用的训练脚本
    samples/            # 两三张样例图（PNG，进库）
```

## 不收进来的东西

- 权重文件（`*.safetensors`，40~80MB）→ 网盘或 Git LFS
- 训练用的私人原图（`*.jpg`）→ 只在本地 `~/.dsh/miyu/refs/`，绝不进任何仓库

`.gitignore` 已经把这些挡在外面，只放行 `samples/*.png`。

## 新增一版的清单

1. 训练完，权重改名成 `<模型名>_<版本>.safetensors`
2. `sha256sum` 结果写进版本卡
3. 出三张样例图放 `samples/`
4. 把实际用的 `train_*.sh` 复制进来
5. 版本卡写清：分辨率 / dim / 步数 / 数据集与张数 / 触发词 / 推荐权重 / 已知缺陷 / 和上一版的差别
6. `git add` 后提交，信息写清是哪一版

## 通用出图参数

- 尺寸 768×1024（3:4，与训练集对齐），LoRA 权重 0.85，steps 28，cfg 6.5，dpmpp_2m + karras
- 想要结构更稳，配 Canny 约束；详细流程见 `~/.dsh/miyu/notes/自训LoRA生图流水线.md`
- 想要成品级、手不出错：拿真照片做 img2img 重绘（denoise 0.5），手随照片

## 这台机器的边界（实测）

- 训练上限：SD1.5 LoRA，832 分辨率、dim 96、显存约 2.8~3.0GB，30 分钟一版
- SDXL 训练不可行：16GB 内存加载底模就被内核 OOM 杀（三次实测），加内存或上云卡才有解
- 手部细节是 SD1.5 的先天短板，局部重绘试过，蒙版落点不可靠，已放弃

## 备注

仓库目前只在本地 `~/.dsh/miyu/limi-models`，没有远端。要推 GitHub 先说一声，公开还是私有也一起定。

## 工具

| 工具 | 作用 |
| --- | --- |
| [`tools/train-status`](tools/train-status/) | 训练监视器浮窗：六页信息、四档自适应尺寸、图片页与 imv 同步翻图 |
