# Skill 发现与信任

该包加载用户级和项目级 Skill，并隔离不可信项目内容。

- `catalog.py`：manifest 读取、`select_frontmatter`、`SkillDefinition`、`SkillCatalog`。
- `project.py`：`discover_project_skills` 和 `ProjectSkillDefinition`。
- `trust.py`：workspace hash、`ProjectSkillTrustStore` 与 `ProjectSkillGate`。
- `__init__.py`：公开发现和选择接口。

未知 frontmatter 字段允许存在；无效 name/description 跳过单个 Skill。项目 Skill 未获信任前不得注入模型上下文。
