# 海景几何

该包为 `../OceanScene.tsx` 提供纯计算能力。

- `sunPosition.ts`：根据视口和时间计算太阳位置。
- `sunPosition.test.ts`：边界和确定性测试。

这里不访问 DOM、不渲染 React，便于视觉组件独立测试。
