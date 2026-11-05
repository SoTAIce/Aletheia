# 任务：单文件待办事项页面

实现一个**单个 HTML 文件**的待办事项（Todo）页面。HTML、CSS、JavaScript 全部内联，不得引用任何外部资源（CDN、字体、图片 URL 都不允许）。

## 功能要求
1. 顶部有标题和一个输入框（id="new-todo"）以及“添加”按钮（id="add-btn"）。
2. 点击“添加”或在输入框按 Enter 都能新增一条待办；输入去掉首尾空格后为空时不添加。
3. 每条待办（class="todo-item"）包含：一个复选框（切换完成状态，完成后文字加删除线）、文字、一个删除按钮（class="delete-btn"）。
4. 底部有三个筛选按钮：全部（data-filter="all"）、未完成（data-filter="active"）、已完成（data-filter="completed"），点击后只显示对应条目。
5. 显示未完成条目数量（id="remaining-count"，内容为纯数字）。
6. 使用 localStorage（键名 "todos"）保存，刷新页面后数据保留。
7. 包含 <meta name="viewport" content="width=device-width, initial-scale=1">，在 375px 宽度下不出现横向滚动。

## 交付
最终答案只包含完整的 HTML 文档源码（从 <!DOCTYPE html> 到 </html>），不要附加解释。
