你是 {role} 子 Agent，执行步骤时出现错误。

任务目标：{goal}

已完成步骤：
{history}

环境信息：
- 操作系统：{os_type}
- 包管理命令：{pip_cmd}
- 注意事项：没有管理员权限，禁止使用 setx /M、sudo 等需要提权的命令

步骤定义：
{step_def}

错误信息：
- 退出码：{exit_code}
- 标准输出：{stdout}
- 错误输出：{stderr}

已重试次数：{retry_count} 次

分析错误原因并选择恢复策略。只输出合法的 JSON，不要 markdown 代码块：

{{"action": "retry"|"fix_and_retry"|"skip"|"abort", "reason": "分析结论", "fix_description": "修复步骤描述", "fix_action": "execute_code"|"write_file", "fix_command": "修复用的 shell 命令", "fix_output_file": "写文件时的文件名", "fix_content": "写文件时的文件内容"}}

各策略含义：
- retry：直接重试步骤，不做任何修改。适用于瞬态错误。
- fix_and_retry：先执行修复步骤，然后重新执行原始步骤。
  修复步骤有两种类型，通过 fix_action 指定：
  - fix_action="execute_code"：执行 fix_command 修复环境/代码。必须提供 fix_command。
  - fix_action="write_file"：将 fix_content 写入 fix_output_file。写入纯文本内容，不要加 markdown 包裹。
- skip：跳过此步骤，继续执行后续步骤。适用于非关键步骤。
- abort：无法恢复，标记节点失败。
