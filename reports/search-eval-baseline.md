# Support Knowledge Engine 检索评测报告

- 生成时间：2026-07-11T02:11:08+08:00
- 用例数：13
- 通过数：13
- 通过率：100.0%

| 用例 | 问题 | 命中文档 | 目标排名 | 禁止产品混入 | 结果 |
|---|---|---:|---:|---:|---:|
| xr-diagnostic-code | 量子灯塔 | 是 | 1 | 否 | 通过 |
| xr-model | XR-100 | 是 | 1 | 否 | 通过 |
| nebula-calibration | uplink calibration | 是 | 1 | 否 | 通过 |
| nebula-model | NS-24 | 是 | 1 | 否 | 通过 |
| generic-echo | 琥珀回声 | 是 | 1 | 否 | 通过 |
| mini-standard-name | AeroCam Mini 2 | 是 | 1 | 否 | 通过 |
| mini-short-name | Aero Mini 2 | 是 | 1 | 否 | 通过 |
| mini-abbreviation | ACM2 | 是 | 1 | 否 | 通过 |
| mini-chinese-alias | 航拍迷你二代 | 是 | 1 | 否 | 通过 |
| mini-propeller | propeller calibration | 是 | 1 | 否 | 通过 |
| mini-error-code | AM2-17 | 是 | 1 | 否 | 通过 |
| pro-error-code | AP2-90 | 是 | 1 | 否 | 通过 |
| pro-thermal-alignment | thermal alignment | 是 | 1 | 否 | 通过 |

## 用例说明

- **xr-diagnostic-code**：目标 `星河路由器_XR-100_用户手册_v1.2_zh-CN.pdf` 第 2 页。中文故障短语
- **xr-model**：目标 `星河路由器_XR-100_用户手册_v1.2_zh-CN.pdf` 第 1 页。型号检索
- **nebula-calibration**：目标 `Nebula-Switch_NS-24_Installation-Guide_v2.0_en-US.pdf` 第 2 页。英文操作短语
- **nebula-model**：目标 `Nebula-Switch_NS-24_Installation-Guide_v2.0_en-US.pdf` 第 1 页。英文型号
- **generic-echo**：目标 `通用故障排查指南_zh-CN.pdf` 第 2 页。待确认文档仍可检索
- **mini-standard-name**：目标 `AeroCam-Mini-2_Service-Handbook_v3.1_en-US.pdf` 第 1 页。标准名称
- **mini-short-name**：目标 `AeroCam-Mini-2_Service-Handbook_v3.1_en-US.pdf` 第 1 页。英文常见名称别名
- **mini-abbreviation**：目标 `AeroCam-Mini-2_Service-Handbook_v3.1_en-US.pdf` 第 1 页。型号缩写
- **mini-chinese-alias**：目标 `AeroCam-Mini-2_Service-Handbook_v3.1_en-US.pdf` 第 1 页。中文同义名称
- **mini-propeller**：目标 `AeroCam-Mini-2_Service-Handbook_v3.1_en-US.pdf` 第 2 页。容易混淆产品的维修步骤
- **mini-error-code**：目标 `AeroCam-Mini-2_Service-Handbook_v3.1_en-US.pdf` 第 3 页。Mini 错误码
- **pro-error-code**：目标 `AeroCam-Pro-2_Service-Handbook_v4.0_en-US.pdf` 第 2 页。Pro 错误码
- **pro-thermal-alignment**：目标 `AeroCam-Pro-2_Service-Handbook_v4.0_en-US.pdf` 第 2 页。专业型号专属诊断短语
