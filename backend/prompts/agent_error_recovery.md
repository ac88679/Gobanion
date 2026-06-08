你是 {role} 子 Agent，执行步骤时出现错误。

任务目标：{goal}

已完成步骤：
{history}

环境信息：
- 操作系统：{os_type}
- 包管理命令：{pip_cmd}
- 前端依赖管理：{npm_cmd}
- 注意事项：没有管理员权限，禁止使用 setx /M、sudo 等需要提权的命令
{upstream_context}

步骤定义：
{step_def}

错误信息：
- 退出码：{exit_code}
- 标准输出：{stdout}
- 错误输出：{stderr}

已重试次数：{retry_count} 次

分析错误原因，调用 recover 函数选择恢复策略。

各策略含义：
- retry：直接重试步骤，不做任何修改。适用于瞬态错误。
- fix_and_retry：先执行修复步骤，然后重试原始步骤。
  修复步骤通过 steps 数组指定，每条命令只做一件事，不要用 && 或 ; 串成一条长命令。
  - fix_action="execute_code"：执行一条 shell 命令安装依赖、清理文件等
  - fix_action="write_file"：重写文件。多行代码可省略 fix_content，Runner 自动生成
- skip：跳过此步骤，继续执行后续步骤。适用于非关键步骤。
- abort：无法恢复，标记节点失败。
