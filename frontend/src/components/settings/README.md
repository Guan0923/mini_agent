# 用户设置模块

该包负责设置数据的加载、编辑、脏状态保护和分区渲染。

- `contracts.ts`：设置分区、Provider draft、默认值、`normalizeSettings` 和失败回退。
- `useUserSettingsState.ts`：加载/保存 Profile、Agent、Runtime、Provider，模型发现和激活/删除流程。
- `GeneralSettingsSections.tsx`：个人简介、Agent 和 Runtime 表单。
- `ProviderSettingsSections.tsx`：Provider 新增与管理表单。

`../UserSettingsModal.tsx` 是公开组件门面。状态 Hook 依赖 API；展示文件只通过 Hook 返回的命令修改状态，避免重复请求逻辑。
