export function authHeaders() {
  const token = localStorage.getItem('p2a_token');
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      ...authHeaders(),
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
      ...options.headers,
    },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `请求失败 (${response.status})`);
  }
  return response.status === 204 ? null : response.json();
}

export function coverKind(title = '') {
  if (title.includes('房贷') || title.includes('计算器')) return 'mortgage';
  if (title.includes('待办') || title.includes('看板') || title.includes('任务')) return 'kanban';
  if (title.includes('主页')) return 'profile';
  if (title.includes('番茄') || title.includes('专注')) return 'pomodoro';
  return 'notes';
}

export const examples = [
  { title: '智能房贷计算器', icon: '⌂', prompt: '做一个智能房贷计算器，输入房价、首付比例、利率和贷款年限，计算月供及总利息' },
  { title: '项目管理看板', icon: '▦', prompt: '做一个任务看板，支持添加任务、在待办/进行中/已完成之间推进，刷新后数据还在' },
  { title: '个人主页', icon: '◉', prompt: '做一个个人主页，展示头像、简介、技能和作品链接，支持编辑并保存' },
  { title: '专注番茄钟', icon: '◷', prompt: '做一个番茄钟，25分钟专注+5分钟休息，可以暂停重置并记录今日完成次数' },
  { title: '灵感笔记本', icon: '▤', prompt: '做一个灵感笔记本，支持新建、搜索、颜色分类和删除，数据自动保存' },
];

export const descriptions = {
  mortgage: '输入房价、首付与利率，快速估算月供和总利息。',
  kanban: '添加任务，在待办、进行中与已完成之间推进。',
  profile: '编辑个人简介、技能和作品，生成专属主页。',
  pomodoro: '专注与休息计时，记录每日完成次数。',
  notes: '随手记录灵感，搜索笔记并自动保存。',
};
