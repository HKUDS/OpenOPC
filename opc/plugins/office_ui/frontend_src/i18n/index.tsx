import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'

export type Locale = 'en' | 'zh-CN'

const STORAGE_KEY = 'openopc_locale'

const en = {
  'app.page.workspace': 'Workspace',
  'app.page.office': 'Office',
  'app.page.org': 'Org',
  'app.page.mapEditor': 'Map editor',
  'app.metric.agents': 'agents',
  'app.metric.skills': 'skills',
  'app.metric.tasks': 'tasks',
  'language.label': 'Language',
  'language.english': 'EN',
  'language.chinese': '简',
  'outdoor.title': 'Outdoor lighting override',
  'outdoor.auto': 'Outdoor auto',
  'outdoor.day': 'Outdoor day',
  'outdoor.night': 'Outdoor night',
  'theme.midnight': 'Midnight',
  'theme.neon': 'Neon',
  'theme.paper': 'Paper',
  'theme.retro': 'Retro',
  'theme.terminal': 'Terminal',
  'theme.cozy': 'Cozy',
  'theme.openopc': 'OpenOPC',
  'dev.tools': 'Developer Tools',
  'dev.connection': 'Connection',
  'dev.evolution': 'Evolution Pipeline',
  'dev.events': 'Events',
  'dev.channels': 'Channels',
  'dev.phase.trace': 'Trace',
  'dev.phase.reflect': 'Reflect',
  'dev.phase.synthesize': 'Synthesize',
  'dev.phase.practice': 'Practice',
  'dev.phase.lifecycle': 'Lifecycle',
  'project.switch': 'Switch project',
  'project.new': 'New project',
  'project.delete': 'Delete project',
  'project.deleteButton': 'Del',
  'project.placeholder': 'project-name',
  'project.confirmTitle': 'Delete project "{name}"?',
  'project.confirmBody': 'All sessions, messages, tasks, and agent data in this project will be permanently deleted. This action cannot be undone.',
  'common.cancel': 'Cancel',
  'common.delete': 'Delete',
  'common.yes': 'Yes',
  'common.no': 'No',
  'common.loading': 'Loading...',
  'common.ready': 'ready',
  'common.autoSaved': 'Auto-saved',
  'common.active': 'active',
  'common.idle': 'idle',
  'common.general': 'general',
  'common.state': 'State',
  'common.tool': 'Tool',
  'common.task': 'Task',
  'common.seat': 'Seat',
  'common.role': 'Role',
  'common.employee': 'Employee',
  'common.manager': 'Manager',
  'common.origin': 'Origin',
  'common.projection': 'Projection',
  'common.gate': 'Gate',
  'common.runtime': 'Runtime',
  'office.modeHint.org': 'Manage your team in the Org tab',
  'office.modeHint.switch': 'Switch to Org mode to create or manage agents',
  'office.offices': 'Offices',
  'office.activeAgents': 'Active Agents',
  'office.rename': 'Rename',
  'office.noSeat': 'no seat',
  'office.moveHere': '+ Move here',
  'office.characters': 'Characters',
  'office.hideSub': 'hide sub',
  'office.showSub': 'show sub',
  'office.hideSubagents': 'Hide sub-agents',
  'office.showSubagents': 'Show sub-agents',
  'office.showSidePanel': 'Show side panel',
  'office.hideSidePanel': 'Hide side panel',
  'office.removeAgent': 'Remove {name}',
  'office.deleteQuestion': 'Delete?',
  'office.emptyAgents': 'No agents yet — click a template above to spawn one.',
  'session.newChat': 'New Chat',
  'session.secretary': 'Secretary',
  'session.search': 'Search...',
  'session.activity': 'Activity',
  'session.noSessions': 'No sessions yet',
  'session.today': 'Today',
  'session.yesterday': 'Yesterday',
  'session.earlier': 'Earlier',
  'session.runtimeSuffix': 'Runtime Sessions',
  'session.justNow': 'just now',
  'session.minutes': '{count}m',
  'session.hours': '{count}h',
  'session.days': '{count}d',
  'session.companyRuntime': 'Company runtime',
  'session.expand': 'Expand',
  'session.collapse': 'Collapse',
  'session.subTask': '{count} sub-task',
  'session.subTasks': '{count} sub-tasks',
  'workspace.selectRuntimeSession': 'Select a Runtime Session on the left to view its Work Item board.',
  'workspace.noWorkItems': 'No work items yet — start delegation to populate this board.',
  'workspace.clickEditTitle': 'Click to edit session title',
  'workspace.channel.secretary': 'Secretary',
  'workspace.channel.activity': 'Activity',
  'workspace.confirmNoRoles': 'Your org has no roles defined.\n\nSet up at least one role before running a task.\n\nGo to Org tab now?',
  'workspace.confirmNoDecider': 'Your organization has multiple top-level roles but no final decider selected.\n\nChoose one final decider in the Org tab before running a task.\n\nGo to Org tab now?',
  'workspace.warningNoTeams': 'No runtime teams defined — the system will auto-generate from your roles',
  'workspace.warningVacantRoles': '{count} role(s) have no employees: {names}',
  'workspace.confirmWarnings': 'Before running this task:\n\n{warnings}\n\nRun anyway?',
  'kanban.addTask': 'Add task',
  'kanban.taskTitle': 'Task title...',
  'kanban.startTask': 'Start task',
  'kanban.startWorkItem': 'Start Work Item',
  'kanban.upstreamDeps': '{count} upstream dep(s)',
  'kanban.dep': '{count} dep',
  'kanban.crossOffice': 'Cross-office',
  'kanban.executionTurn': 'Execution Turn: {id}',
  'kanban.column.Todo': 'Todo',
  'kanban.column.In Progress': 'In Progress',
  'kanban.column.Done': 'Done',
  'kanban.column.Ready': 'Ready',
  'kanban.column.Running': 'Running',
  'kanban.column.Review': 'Review',
  'kanban.column.Approved': 'Approved',
  'kanban.status.todo': 'To do',
  'kanban.status.in_progress': 'In progress',
  'kanban.status.in_review': 'In review',
  'kanban.status.done': 'Done',
  'kanban.status.running': 'Running',
  'kanban.status.idle': 'Idle',
  'kanban.status.blocked': 'Blocked',
  'kanban.status.awaiting_peer': 'Awaiting',
  'kanban.status.awaiting_manager_review': 'Mgr Review',
  'kanban.status.awaiting_human': 'Human Review',
  'kanban.status.awaiting_review': 'In Review',
  'kanban.status.failed': 'Failed',
  'kanban.status.cancelled': 'Cancelled',
  'agent.status.idle': 'Idle',
  'agent.status.reflecting': 'Thinking...',
  'agent.status.tool_active': 'Running tool',
  'agent.summary.active': '{active}/{total} active',
  'agent.summary.count': '{count} agent',
  'agent.summary.countPlural': '{count} agents',
  'org.loading': 'Loading organization data...',
  'org.company': 'Company',
  'org.customOrg': 'Custom org',
  'org.corporateCompany': 'Corporate company',
  'org.kind.saved': 'Saved org',
  'org.kind.corporate': 'Corporate',
  'org.state.editableSaved': 'Editable saved architecture',
  'org.state.editableDraft': 'Editable draft architecture',
  'org.state.readOnly': 'Built-in read-only architecture',
  'org.organization': 'Organization',
  'org.corporate': 'Corporate',
  'org.newOrganization': 'New organization',
  'org.roles': 'roles',
  'org.employees': 'employees',
  'org.runtimeTeams': 'runtime teams',
  'org.tab.team': 'Team',
  'org.tab.runtime': 'Runtime',
  'org.tab.architecture': 'Architecture',
  'org.tab.employees': 'Employees',
  'org.createdToast': 'Created {name} and saved automatically',
  'org.architectureApplied': 'Architecture applied successfully',
}

type MessageKey = keyof typeof en

const zh: Record<MessageKey, string> = {
  'app.page.workspace': '工作台',
  'app.page.office': '办公室',
  'app.page.org': '组织',
  'app.page.mapEditor': '地图编辑',
  'app.metric.agents': 'Agent',
  'app.metric.skills': '技能',
  'app.metric.tasks': '任务',
  'language.label': '语言',
  'language.english': 'EN',
  'language.chinese': '简',
  'outdoor.title': '户外光照',
  'outdoor.auto': '户外自动',
  'outdoor.day': '户外白天',
  'outdoor.night': '户外夜晚',
  'theme.midnight': '午夜',
  'theme.neon': '霓虹',
  'theme.paper': '纸张',
  'theme.retro': '复古',
  'theme.terminal': '终端',
  'theme.cozy': '舒适',
  'theme.openopc': 'OpenOPC',
  'dev.tools': '开发工具',
  'dev.connection': '连接',
  'dev.evolution': '进化流水线',
  'dev.events': '事件',
  'dev.channels': '频道',
  'dev.phase.trace': '追踪',
  'dev.phase.reflect': '反思',
  'dev.phase.synthesize': '合成',
  'dev.phase.practice': '练习',
  'dev.phase.lifecycle': '生命周期',
  'project.switch': '切换项目',
  'project.new': '新建项目',
  'project.delete': '删除项目',
  'project.deleteButton': '删',
  'project.placeholder': '项目名',
  'project.confirmTitle': '删除项目“{name}”？',
  'project.confirmBody': '此项目中的所有会话、消息、任务和 Agent 数据都会被永久删除。此操作无法撤销。',
  'common.cancel': '取消',
  'common.delete': '删除',
  'common.yes': '是',
  'common.no': '否',
  'common.loading': '加载中...',
  'common.ready': '就绪',
  'common.autoSaved': '已自动保存',
  'common.active': '活跃',
  'common.idle': '空闲',
  'common.general': '通用',
  'common.state': '状态',
  'common.tool': '工具',
  'common.task': '任务',
  'common.seat': '座位',
  'common.role': '角色',
  'common.employee': '员工',
  'common.manager': '经理',
  'common.origin': '来源',
  'common.projection': '投影',
  'common.gate': '关卡',
  'common.runtime': '运行时',
  'office.modeHint.org': '在“组织”页管理你的团队',
  'office.modeHint.switch': '切换到“组织”模式以创建或管理 Agent',
  'office.offices': '办公室',
  'office.activeAgents': '活跃 Agent',
  'office.rename': '重命名',
  'office.noSeat': '未分配座位',
  'office.moveHere': '+ 移到这里',
  'office.characters': '角色',
  'office.hideSub': '隐藏子项',
  'office.showSub': '显示子项',
  'office.hideSubagents': '隐藏子 Agent',
  'office.showSubagents': '显示子 Agent',
  'office.showSidePanel': '显示侧栏',
  'office.hideSidePanel': '隐藏侧栏',
  'office.removeAgent': '移除 {name}',
  'office.deleteQuestion': '删除？',
  'office.emptyAgents': '还没有 Agent，点击上方模板生成一个。',
  'session.newChat': '新聊天',
  'session.secretary': '秘书',
  'session.search': '搜索...',
  'session.activity': '动态',
  'session.noSessions': '还没有会话',
  'session.today': '今天',
  'session.yesterday': '昨天',
  'session.earlier': '更早',
  'session.runtimeSuffix': '运行时会话',
  'session.justNow': '刚刚',
  'session.minutes': '{count} 分钟',
  'session.hours': '{count} 小时',
  'session.days': '{count} 天',
  'session.companyRuntime': '公司运行时',
  'session.expand': '展开',
  'session.collapse': '折叠',
  'session.subTask': '{count} 个子任务',
  'session.subTasks': '{count} 个子任务',
  'workspace.selectRuntimeSession': '在左侧选择一个运行时会话来查看它的工作项看板。',
  'workspace.noWorkItems': '还没有工作项，开始委派后会填充此看板。',
  'workspace.clickEditTitle': '点击编辑会话标题',
  'workspace.channel.secretary': '秘书',
  'workspace.channel.activity': '动态',
  'workspace.confirmNoRoles': '你的组织还没有定义角色。\n\n运行任务前请至少设置一个角色。\n\n现在前往“组织”页吗？',
  'workspace.confirmNoDecider': '你的组织有多个顶层角色，但尚未选择最终决策者。\n\n运行任务前请在“组织”页选择一个最终决策者。\n\n现在前往“组织”页吗？',
  'workspace.warningNoTeams': '尚未定义运行时团队，系统会根据角色自动生成',
  'workspace.warningVacantRoles': '{count} 个角色没有员工：{names}',
  'workspace.confirmWarnings': '运行此任务前：\n\n{warnings}\n\n仍然运行吗？',
  'kanban.addTask': '添加任务',
  'kanban.taskTitle': '任务标题...',
  'kanban.startTask': '启动任务',
  'kanban.startWorkItem': '启动工作项',
  'kanban.upstreamDeps': '{count} 个上游依赖',
  'kanban.dep': '{count} 依赖',
  'kanban.crossOffice': '跨办公室',
  'kanban.executionTurn': '执行轮次：{id}',
  'kanban.column.Todo': '待办',
  'kanban.column.In Progress': '进行中',
  'kanban.column.Done': '完成',
  'kanban.column.Ready': '就绪',
  'kanban.column.Running': '运行中',
  'kanban.column.Review': '评审',
  'kanban.column.Approved': '已批准',
  'kanban.status.todo': '待办',
  'kanban.status.in_progress': '进行中',
  'kanban.status.in_review': '评审中',
  'kanban.status.done': '完成',
  'kanban.status.running': '运行中',
  'kanban.status.idle': '空闲',
  'kanban.status.blocked': '阻塞',
  'kanban.status.awaiting_peer': '等待同伴',
  'kanban.status.awaiting_manager_review': '经理评审',
  'kanban.status.awaiting_human': '人工评审',
  'kanban.status.awaiting_review': '评审中',
  'kanban.status.failed': '失败',
  'kanban.status.cancelled': '已取消',
  'agent.status.idle': '空闲',
  'agent.status.reflecting': '思考中...',
  'agent.status.tool_active': '运行工具',
  'agent.summary.active': '{active}/{total} 活跃',
  'agent.summary.count': '{count} 个 Agent',
  'agent.summary.countPlural': '{count} 个 Agent',
  'org.loading': '正在加载组织数据...',
  'org.company': '公司',
  'org.customOrg': '自定义组织',
  'org.corporateCompany': '企业公司',
  'org.kind.saved': '已保存组织',
  'org.kind.corporate': '企业',
  'org.state.editableSaved': '可编辑的已保存架构',
  'org.state.editableDraft': '可编辑草稿架构',
  'org.state.readOnly': '内置只读架构',
  'org.organization': '组织',
  'org.corporate': '企业',
  'org.newOrganization': '新建组织',
  'org.roles': '角色',
  'org.employees': '员工',
  'org.runtimeTeams': '运行时团队',
  'org.tab.team': '团队',
  'org.tab.runtime': '运行时',
  'org.tab.architecture': '架构',
  'org.tab.employees': '员工',
  'org.createdToast': '已创建 {name} 并自动保存',
  'org.architectureApplied': '架构已成功应用',
}

const dictionaries = { en, 'zh-CN': zh } satisfies Record<Locale, Record<MessageKey, string>>

interface I18nContextValue {
  locale: Locale
  setLocale: (locale: Locale) => void
  t: (key: MessageKey, params?: Record<string, string | number>) => string
  translateMaybe: (namespace: string, value: string | undefined | null) => string
}

const I18nContext = createContext<I18nContextValue | null>(null)

function readInitialLocale(): Locale {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    if (stored === 'en' || stored === 'zh-CN') return stored
  } catch {
    // private mode
  }
  try {
    return navigator.language.toLowerCase().startsWith('zh') ? 'zh-CN' : 'en'
  } catch {
    return 'en'
  }
}

function interpolate(template: string, params?: Record<string, string | number>): string {
  if (!params) return template
  return template.replace(/\{(\w+)\}/g, (_, key: string) => (
    Object.prototype.hasOwnProperty.call(params, key) ? String(params[key]) : `{${key}}`
  ))
}

export function I18nProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(() => readInitialLocale())

  useEffect(() => {
    document.documentElement.lang = locale === 'zh-CN' ? 'zh-CN' : 'en'
  }, [locale])

  const setLocale = useCallback((next: Locale) => {
    setLocaleState(next)
    try {
      localStorage.setItem(STORAGE_KEY, next)
      document.documentElement.lang = next === 'zh-CN' ? 'zh-CN' : 'en'
    } catch {
      // private mode
    }
  }, [])

  const t = useCallback<I18nContextValue['t']>((key, params) => {
    const template = dictionaries[locale][key] ?? dictionaries.en[key] ?? key
    return interpolate(template, params)
  }, [locale])

  const translateMaybe = useCallback<I18nContextValue['translateMaybe']>((namespace, value) => {
    const normalized = String(value ?? '').trim()
    if (!normalized) return ''
    const key = `${namespace}.${normalized}` as MessageKey
    return dictionaries[locale][key] ?? normalized
  }, [locale])

  const value = useMemo<I18nContextValue>(() => ({ locale, setLocale, t, translateMaybe }), [locale, setLocale, t, translateMaybe])

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>
}

export function useI18n(): I18nContextValue {
  const ctx = useContext(I18nContext)
  if (!ctx) throw new Error('useI18n must be used within I18nProvider')
  return ctx
}
