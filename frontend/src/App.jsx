import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, authHeaders, coverKind, descriptions, examples } from './api.js';

const stages = ['PM 拆解', '设计规范', '桌面端生成', '代码审查', '移动端版本', '静态走查', '回复整理'];

function previewDocument(code, appState) {
  const serializedState = JSON.stringify(appState || null).replace(/<\//g, '<\\/');
  const bridge = `<script>
(function () {
  var initial = ${serializedState};
  if (initial && typeof initial === 'object') {
    Object.keys(initial).forEach(function (key) { localStorage.setItem(key, initial[key]); });
  }
  var timer;
  function collect() {
    var state = {};
    for (var i = 0; i < localStorage.length; i++) {
      var key = localStorage.key(i);
      state[key] = localStorage.getItem(key);
    }
    return state;
  }
  function emit() { parent.postMessage({ type: 'p2a:state', state: collect() }, '*'); }
  function schedule() { clearTimeout(timer); timer = setTimeout(emit, 800); }
  ['setItem', 'removeItem', 'clear'].forEach(function (method) {
    var original = localStorage[method].bind(localStorage);
    localStorage[method] = function () {
      var result = original.apply(null, arguments);
      schedule();
      return result;
    };
  });
  addEventListener('beforeunload', emit);
})();
</script>`;
  const source = code || '';
  return /<head(?:\\s[^>]*)?>/i.test(source)
    ? source.replace(/<head(?:\\s[^>]*)?>/i, (head) => `${head}${bridge}`)
    : `${bridge}${source}`;
}

function readError(error) {
  return error instanceof Error ? error.message : '请求失败，请稍后重试';
}

function isHtmlDocument(content) {
  const text = String(content || '').trim().replace(/^```(?:html)?\s*/i, '').replace(/\s*```$/, '').trim();
  return /^(?:<!doctype\s+html\b|<html\b)/i.test(text) && /<\/html\s*>$/i.test(text);
}

function filterConversationMessages(messages) {
  return (messages || []).filter((message) => !(message.role === 'assistant' && isHtmlDocument(message.content)));
}

function useLocation() {
  const [location, setLocation] = useState(() => ({
    path: window.location.pathname,
    search: window.location.search,
    hash: window.location.hash,
  }));
  useEffect(() => {
    const update = () => setLocation({ path: window.location.pathname, search: window.location.search, hash: window.location.hash });
    window.addEventListener('popstate', update);
    return () => window.removeEventListener('popstate', update);
  }, []);
  const navigate = useCallback((target, replace = false) => {
    if (replace) window.history.replaceState(null, '', target);
    else window.history.pushState(null, '', target);
    setLocation({ path: window.location.pathname, search: window.location.search, hash: window.location.hash });
  }, []);
  return [location, navigate];
}

function Logo({ onClick }) {
  return (
    <button className="brand" type="button" onClick={onClick} aria-label="Prompt2App 首页">
      <span className="brand-mark">P</span>
      <span>Prompt<span className="brand-accent">2</span>App</span>
    </button>
  );
}

function Header({ path, user, syncState, onNavigate, onProjects, onLogin, onLogout }) {
  const workbench = path === '/app';
  return (
    <header className={`topbar ${workbench ? 'topbar-workbench' : ''}`}>
      <div className="topbar-main">
        <Logo onClick={() => onNavigate('/')} />
        <nav className="main-nav" aria-label="主导航">
          <button className={path === '/' ? 'active' : ''} onClick={() => onNavigate('/')}>首页</button>
          <button onClick={() => onNavigate('/#showcase')}>预置成品</button>
          <button className={path === '/dashboard' ? 'active' : ''} onClick={onProjects}>我的项目</button>
        </nav>
        <div className="account-actions">
          {user && syncState && <span className={`sync-label ${syncState}`}>{syncState === 'syncing' ? '同步中' : '已同步'}</span>}
          {user ? (
            <>
              <span className="user-chip">{user.username}</span>
              <button className="button button-quiet button-small" onClick={onLogout}>退出</button>
            </>
          ) : <button className="button button-primary button-small" onClick={onLogin}>登录 / 注册</button>}
        </div>
      </div>
    </header>
  );
}

function TemplateCover({ title, compact = false }) {
  const kind = coverKind(title);
  return (
    <div className={`template-cover cover-${kind} ${compact ? 'compact' : ''}`} aria-hidden="true">
      <div className="mock-window">
        <div className="mock-toolbar"><i /><i /><i /><span>{title}</span></div>
        {kind === 'mortgage' && (
          <div className="mock-mortgage">
            <div className="mock-inputs"><i /><i /><i /></div>
            <div className="mock-payment"><small>每月还款</small><b>¥ 8,420</b></div>
            <div className="mock-chart"><i /><i /><i /><i /><i /><i /><i /></div>
          </div>
        )}
        {kind === 'kanban' && (
          <div className="mock-columns">{['待办', '进行中', '完成'].map((label, index) => (
            <div className="mock-column" key={label}><small>{label}</small><i /><i className={index === 1 ? 'highlight' : ''} /></div>
          ))}</div>
        )}
        {kind === 'profile' && (
          <div className="mock-profile"><i className="mock-avatar" /><span><b /><i /><i /></span><div className="mock-tiles"><i /><i /><i /></div></div>
        )}
        {kind === 'pomodoro' && (
          <div className="mock-timer"><i className="timer-ring"><b>25:00</b></i><span><i /><i /></span><small>专注时刻</small></div>
        )}
        {kind === 'notes' && (
          <div className="mock-notes"><i /><i /><i /><i /><i /></div>
        )}
      </div>
      <span className="cover-orbit orbit-one" />
      <span className="cover-orbit orbit-two" />
    </div>
  );
}

function ShowcaseCard({ item, onOpen, onUsePrompt, canCreate }) {
  const kind = coverKind(item.title);
  const example = examples.find((entry) => coverKind(entry.title) === kind) || examples[4];
  return (
    <article className="showcase-card">
      <button className="showcase-open" type="button" onClick={() => onOpen(item.id)} aria-label={`打开${item.title}`}>
        <TemplateCover title={item.title} />
        <div className="showcase-content">
          <div className="showcase-title-row">
            <h3>{item.title}</h3>
            <span className="category-tag">{({ mortgage: '计算', kanban: '效率', profile: '个人主页', pomodoro: '专注', notes: '笔记' })[kind]}</span>
          </div>
          <p>{descriptions[kind]}</p>
        </div>
      </button>
      <div className="showcase-actions">
        <button type="button" className="text-action" onClick={() => onOpen(item.id)}>体验成品 <span aria-hidden="true">↗</span></button>
        {canCreate && <button type="button" className="text-action" onClick={() => onUsePrompt(example.prompt)}>用它开始创作</button>}
      </div>
    </article>
  );
}

function LoadingCards() {
  return <div className="showcase-grid">{[0, 1, 2].map((item) => <div className="loading-card" key={item}><div className="skeleton cover-skeleton" /><div className="skeleton line-skeleton" /><div className="skeleton short-skeleton" /></div>)}</div>;
}

function HomePage({ user, showcase, showcaseLoading, showcaseError, prompt, setPrompt, busy, onCreate, onUsePrompt, onOpen, onRetry, onLogin, apiStatus }) {
  const inputRef = useRef(null);
  const length = prompt.length;
  const isGeneratingElsewhere = Boolean(busy?.busy);
  const fill = (text) => {
    setPrompt(text);
    requestAnimationFrame(() => {
      inputRef.current?.focus();
      inputRef.current?.setSelectionRange(text.length, text.length);
    });
  };
  return (
    <main>
      <section className="hero">
        <div className="hero-grid" />
        <div className="hero-inner">
          <div className={`service-pill ${apiStatus}`}>
            <i />
            {apiStatus === 'online' ? '多智能体应用创作空间' : apiStatus === 'loading' ? '正在连接后端服务…' : '后端暂时无法连接'}
          </div>
          <h1>把灵感，变成<br /><span>真正能用的应用</span></h1>
          <p className="hero-copy">{user
            ? <>描述你的想法，多个 AI Agent 将协作编写代码、检查质量，<br className="desktop-break" />并在沙盒中即时运行。</>
            : <>首页和预置模板均可直接浏览体验；登录后即可创建自己的应用。</>}</p>
          {user ? <>
            <form className="composer" onSubmit={(event) => { event.preventDefault(); onCreate(); }}>
              <label className="sr-only" htmlFor="prompt-input">应用需求描述</label>
              <textarea
                ref={inputRef}
                id="prompt-input"
                maxLength={400}
                rows={3}
                value={prompt}
                onChange={(event) => setPrompt(event.target.value)}
                onKeyDown={(event) => {
                  if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
                    event.preventDefault();
                    onCreate();
                  }
                }}
                placeholder="描述你想做的应用，例如：一个记录每月支出、按类别汇总并支持筛选的记账本"
              />
              <div className="composer-footer">
                <span aria-live="polite">{length}/400 <span className="footer-divider">·</span> ⌘/Ctrl + Enter 生成</span>
                <button className="button button-primary" type="submit" disabled={prompt.trim().length < 4 || isGeneratingElsewhere}>
                  {isGeneratingElsewhere ? '其他应用生成中…' : '开始创作'} <span aria-hidden="true">→</span>
                </button>
              </div>
            </form>
            <div className="quick-prompts">
              <span>试试这些灵感</span>
              {examples.filter((example) => coverKind(example.title) !== 'profile').slice(0, 4).map((example) => <button key={example.title} onClick={() => fill(example.prompt)} type="button">{example.icon} {example.title}</button>)}
            </div>
          </> : <div className="guest-create-gate">
            <span>预置模板可直接体验；登录后即可创建和保存自己的应用。</span>
            <button className="button button-primary" type="button" onClick={onLogin}>登录后开始创作 <span aria-hidden="true">→</span></button>
          </div>}
          {isGeneratingElsewhere && <div className="busy-banner" role="status">另一个应用正在生成「{busy.title || '应用'}」，完成后即可开始创作。</div>}
          {user && <div className="hero-proof"><span><i>✦</i> 从描述到可运行原型</span><span><i>⌘</i> 每次生成都会保存版本</span><span><i>◈</i> 深空主题工作台</span></div>}
        </div>
      </section>

      <section id="showcase" className="section showcase-section">
        <div className="section-heading">
        <div><div className="eyebrow">Template gallery</div><h2>从一个好例子开始</h2><p>{user ? '先体验预置成品，也可以把示例描述带入创作框继续定制。' : '浏览并体验这些预置成品；登录后即可创建自己的应用。'}</p></div>
        <span className="count-pill">{showcase.length} 个可交互模板</span>
        </div>
        {showcaseLoading ? <LoadingCards /> : showcaseError ? (
          <div className="empty-panel error-panel" role="alert"><div className="empty-icon">!</div><h3>预置应用暂时无法加载</h3><p>{showcaseError}</p><button className="button button-secondary" onClick={onRetry}>重新加载</button></div>
        ) : showcase.length ? (
          <div className="showcase-grid">{showcase.map((item) => <ShowcaseCard key={item.id} item={item} onOpen={onOpen} onUsePrompt={onUsePrompt} canCreate={Boolean(user)} />)}</div>
        ) : <div className="empty-panel"><div className="empty-icon">◇</div><h3>还没有公开的预置应用</h3><p>{user ? '登录后可以从首页描述需求，创建自己的应用。' : '预置应用暂不可用，请稍后重试。'}</p></div>}
      </section>

      {user && <section className="workflow-band">
        <div className="workflow-inner"><div><div className="eyebrow">Built with agents</div><h2>从想法到上线预览，只需几步</h2></div>
          <div className="workflow-steps">{[['01', '描述想法'], ['02', 'Agent 协作'], ['03', '实时预览'], ['04', '持续迭代']].map(([number, label]) => <div className="workflow-step" key={number}><span>{number}</span><b>{label}</b></div>)}</div>
        </div>
      </section>}
    </main>
  );
}

function DashboardPage({ user, showcase, sessions, loading, error, onRefresh, onOpen, onDelete, onLogin, onCreate }) {
  return (
    <main className="page-shell dashboard-page">
      <div className="page-heading">
        <div><div className="eyebrow">Your workspace</div><h1>{user ? '我的项目' : '探索项目与预置成品'}</h1><p>{user ? '管理你创建的应用，继续迭代或查看历史版本。' : '登录后即可创建应用、保存项目并在不同设备继续创作。'}</p></div>
        <div className="heading-actions"><button className="button button-secondary" onClick={onRefresh} disabled={loading}>{loading ? <><span className="spinner" /> 刷新中</> : '刷新列表'}</button>{user ? <button className="button button-primary" onClick={() => onCreate()}>＋ 新建应用</button> : <button className="button button-primary" onClick={onLogin}>登录 / 注册</button>}</div>
      </div>
      {!user && <div className="guest-callout"><span className="callout-icon">✦</span><span><b>游客模式</b><small>可直接打开预置应用体验；登录后可创建、保存和迭代自己的项目。</small></span><button className="text-action" onClick={onLogin}>登录继续 →</button></div>}
      {error && <div className="inline-error" role="alert">{error}</div>}
      {user && <section id="mine" className="dashboard-section my-projects-section"><div className="subsection-title"><div><h2>我的项目 <span className="count-pill">个人工作区</span></h2><p>{sessions.length} 个项目</p></div></div>
        {loading ? <div className="project-grid">{[0, 1, 2].map((id) => <div className="loading-card" key={id}><div className="skeleton cover-skeleton" /><div className="skeleton line-skeleton" /><div className="skeleton short-skeleton" /></div>)}</div> : sessions.length ? (
          <div className="project-grid">{sessions.map((session) => <article className="project-card" key={session.id}>
            <button type="button" className="project-open" onClick={() => onOpen(session.id)}><TemplateCover title={session.title} compact /><div className="project-info"><h3>{session.title}</h3><p>最近更新 {formatDate(session.updated_at)}</p></div></button>
            <div className="project-actions"><button className="text-action" onClick={() => onOpen(session.id)}>打开工作台 <span aria-hidden="true">→</span></button><button className="icon-button danger-hover" aria-label={`删除${session.title}`} title="删除项目" onClick={() => onDelete(session.id)}>⌫</button></div>
          </article>)}</div>
        ) : <div className="empty-panel"><div className="empty-icon">⌘</div><h3>还没有自己的应用</h3><p>描述一个想法，AI Agent 会帮你生成可运行的应用原型。</p><button className="button button-primary" onClick={() => onCreate()}>开始创建</button></div>}
      </section>}
      <section className="dashboard-section">
        <div className="subsection-title"><div><h2>{user ? '灵感模板' : '预置成品'}</h2><p>{user ? '打开体验，或将示例描述带入创作框。' : '游客可直接打开并体验这些预置应用。'}</p></div></div>
        {showcase.length ? <div className="showcase-grid">{showcase.map((item) => <ShowcaseCard key={item.id} item={item} onOpen={onOpen} onUsePrompt={onCreate} canCreate={Boolean(user)} />)}</div> : <div className="empty-panel"><h3>预置应用暂不可用</h3><p>请稍后刷新重试。</p></div>}
      </section>
    </main>
  );
}

function formatDate(value) {
  if (!value) return '刚刚';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function LoginModal({ onClose, onSubmit }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  async function submit(event) {
    event.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setError('');
    try {
      await onSubmit(username, password);
    } catch (exception) {
      setError(readError(exception));
    } finally {
      setSubmitting(false);
    }
  }
  useEffect(() => {
    const closeOnEscape = (event) => { if (event.key === 'Escape' && !submitting) onClose(); };
    window.addEventListener('keydown', closeOnEscape);
    return () => window.removeEventListener('keydown', closeOnEscape);
  }, [onClose, submitting]);
  return (
    <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget && !submitting) onClose(); }}>
      <section className="login-modal" role="dialog" aria-modal="true" aria-labelledby="login-title">
        <button className="modal-close" type="button" onClick={onClose} aria-label="关闭登录窗口">×</button>
        <span className="brand-mark modal-mark">P</span>
        <div className="eyebrow">Welcome back</div>
        <h2 id="login-title">登录，继续你的创作</h2>
        <p>浏览首页和预置模板无需登录。提交用户名和密码时，若用户名尚未注册会自动创建账号，例如 demo / demo123。</p>
        <form onSubmit={submit}>
          <label>用户名<input autoFocus autoComplete="username" value={username} maxLength={64} onChange={(event) => setUsername(event.target.value)} placeholder="例如 demo" required /></label>
          <label>密码<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="例如 demo123" required /></label>
          {error && <div className="form-error" role="alert">{error}</div>}
          <button className="button button-primary login-submit" disabled={submitting}>{submitting ? <><span className="spinner" /> 登录中…</> : '登录 / 免费注册'}</button>
        </form>
        <button className="modal-back-link" type="button" onClick={onClose}>暂时先逛逛</button>
      </section>
    </div>
  );
}

function Workbench({ user, sessionTitle, currentSessionId, sessionOwned, isPublic, sessions, messages, input, setInput, generating, busy, stepIndex, code, mobileCode, device, setDevice, previewTab, setPreviewTab, workspaceTab, setWorkspaceTab, files, activeFile, fileContent, versions, onOpenSession, onNewSession, onSend, onStop, onLogin, onLoadFile, onRestoreVersion, onDownload, onOpenExternal, onRefreshPreview, previewFrameRef, previewRevision, appState, syncState }) {
  const [expandedStages, setExpandedStages] = useState(false);
  const chatRef = useRef(null);
  useEffect(() => {
    if (chatRef.current) chatRef.current.scrollTop = chatRef.current.scrollHeight;
  }, [messages, generating]);
  const shownCode = device === 'mobile' && mobileCode ? mobileCode : code;
  const previewSrc = useMemo(() => previewDocument(shownCode, appState), [shownCode, appState]);
  const activeStage = stepIndex >= 0 ? stages[stepIndex] : '准备生成';
  return (
    <main className={`workbench ${isPublic ? 'readonly-workbench' : ''}`}>
      {!isPublic && <aside className="workbench-sidebar">
        <div className="sidebar-head"><span>工作区</span><button className="icon-button" onClick={onNewSession} title="新建应用" aria-label="新建应用">＋</button></div>
        <button className="new-project-button" onClick={onNewSession}><span>＋</span> 新建应用</button>
        <div className="sidebar-label">最近项目 <span>{sessions.length}</span></div>
        <div className="sidebar-list">{sessions.map((session) => <button key={session.id} className={`sidebar-project ${Number(session.id) === Number(currentSessionId) ? 'selected' : ''}`} onClick={() => onOpenSession(session.id)} title={session.title}>
          <span className="project-glyph">{coverGlyph(session.title)}</span><span className="sidebar-project-name">{session.title}</span>
          {busy?.busy && Number(busy.session_id) === Number(session.id) && <i className="live-dot" />}
        </button>)}</div>
        {!sessions.length && <div className="sidebar-empty">尚无项目，创建一个新应用开始吧。</div>}
        <div className="sidebar-footer"><span className="user-avatar">{user?.username?.slice(0, 1) || '访'}</span><span>{user?.username || '游客只读'}</span><span className={`sync-dot ${syncState}`} title={syncState === 'syncing' ? '同步中' : '已同步'} /></div>
      </aside>}

      <section className="workbench-content">
        <div className="workbench-heading">
          <div className="workbench-title">
            <div className="eyebrow">{isPublic ? 'Template preview' : 'Prompt2App workspace'}</div>
            <h1>{sessionTitle || '新建应用'}</h1>
          </div>
          {isPublic
            ? <span className="readonly-badge"><i />预置模板 · 只读体验</span>
            : <div className="workbench-tabs" role="tablist" aria-label="工作区视图">
              <button type="button" role="tab" aria-selected={workspaceTab === 'chat'} className={workspaceTab === 'chat' ? 'active' : ''} onClick={() => setWorkspaceTab('chat')}>对话 / 报告</button>
              <button type="button" role="tab" aria-selected={workspaceTab === 'preview'} className={workspaceTab === 'preview' ? 'active' : ''} onClick={() => { setWorkspaceTab('preview'); setPreviewTab('app'); }}>预览</button>
            </div>}
        </div>

        {!isPublic && workspaceTab === 'chat' && <section className="chat-panel">
        {generating && <div className="progress-card">
          <button className="progress-toggle" onClick={() => setExpandedStages((open) => !open)} aria-expanded={expandedStages}>
            <span className="progress-indicator"><i className="pulse-dot" /> Agent 正在协作</span><b>{activeStage}</b><span className="progress-track"><i style={{ width: `${Math.max(8, ((stepIndex + 1) / stages.length) * 100)}%` }} /></span><span className="progress-expand">{expandedStages ? '收起' : '查看阶段'}⌄</span>
          </button>
          {expandedStages && <div className="stage-list">{stages.map((stage, index) => <div className={`stage-item ${index < stepIndex ? 'done' : index === stepIndex ? 'active' : ''}`} key={stage}><i>{index < stepIndex ? '✓' : String(index + 1).padStart(2, '0')}</i><span>{stage}</span><small>{index < stepIndex ? '已完成' : index === stepIndex ? '进行中' : '等待中'}</small></div>)}</div>}
        </div>}
        {busy?.busy && Number(busy.session_id) !== Number(currentSessionId) && <div className="global-busy-note" role="status">「{busy.title || '其他应用'}」正在生成，完成后可继续操作。</div>}
        <div className="message-list" ref={chatRef}>
          {!messages.length && <div className="chat-welcome"><div className="welcome-orb">✦</div><h2>从一句话开始</h2><p>描述你希望应用解决的问题。你可以随时继续对话，调整布局、功能和风格。</p><div className="suggestion-list">{['加一个数据统计面板', '改成更简洁的深色风格', '增加导出和搜索功能'].map((text) => <button key={text} disabled={generating} onClick={() => setInput(text)}>{text}<span>↗</span></button>)}</div></div>}
          {messages.map((message, index) => <div className={`message message-${message.role}`} key={message.id || `${index}-${message.content?.slice(0, 20)}`}>
            {message.role !== 'status' && <span className="message-avatar">{message.role === 'user' ? user?.username?.slice(0, 1) || '我' : '✦'}</span>}
            <div className="message-body">{message.role === 'status' ? <div className="status-message"><i />{message.content}</div> : <><b>{message.role === 'user' ? '你' : 'Prompt2App'}</b><p>{message.content}</p></>}</div>
          </div>)}
          {generating && <div className="typing-indicator"><i /><i /><i /><span>正在生成与检查代码…</span></div>}
        </div>
        <div className="chat-composer-wrap">
          {!user ? <button className="guest-edit-lock" onClick={onLogin}><span>⌑</span><span><b>登录后继续迭代</b><small>游客可以浏览预置应用，登录后即可生成并保存项目。</small></span><span>→</span></button> : busy?.busy && Number(busy.session_id) !== Number(currentSessionId) ? <div className="composer-locked">⏳ 另一个应用正在生成，稍候即可继续。</div> : (
            <form className="chat-composer" onSubmit={(event) => { event.preventDefault(); onSend(); }}>
              <textarea rows={2} value={input} disabled={generating} onChange={(event) => setInput(event.target.value)} onKeyDown={(event) => { if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') { event.preventDefault(); onSend(); } }} placeholder={generating ? '正在等待本轮生成完成…' : '继续迭代，例如：增加搜索功能，并把主色改成蓝色…'} />
              <div>{generating ? <span className="generation-lock" role="status"><i className="pulse-dot" />任务执行中，完成后可继续迭代</span> : <span>⌘/Ctrl + Enter 发送</span>}{generating ? <button className="button button-stop" type="button" onClick={onStop}>停止</button> : <button className="button button-primary send-button" type="submit" disabled={!input.trim() || generating}>发送 <span>↑</span></button>}</div>
            </form>
          )}
          <div className="composer-footnote">AI 生成的代码会在沙盒中运行。请检查内容后再用于生产环境。</div>
        </div>
        </section>}

      {(isPublic || workspaceTab === 'preview') && <section className={`preview-panel ${isPublic ? 'public-preview-panel' : ''}`}>
        <div className="preview-toolbar">
          {!isPublic && <div className="preview-tabs"><button className={previewTab === 'app' ? 'active' : ''} onClick={() => setPreviewTab('app')}>应用</button><button className={previewTab === 'files' ? 'active' : ''} onClick={() => setPreviewTab('files')}>文件 <span>{files.length || ''}</span></button></div>}
          <div className="preview-tools">
            {!isPublic && <select aria-label="版本历史" value="" onChange={(event) => onRestoreVersion(event.target.value)} disabled={!user || !sessionOwned || !currentSessionId || !versions.length}>
              <option value="">版本历史</option>{versions.map((version) => <option key={version.id} value={version.id}>#{version.id} {version.mode} {version.note || ''}</option>)}
            </select>}
            <div className="preview-mode-switch" role="group" aria-label="预览模式">
              <button type="button" className={device === 'desktop' ? 'active' : ''} onClick={() => setDevice('desktop')} aria-label="桌面版本" aria-pressed={device === 'desktop'}><span aria-hidden="true">▰</span>桌面版</button>
              <button type="button" className={device === 'mobile' ? 'active' : ''} onClick={() => setDevice('mobile')} aria-label="手机版本" aria-pressed={device === 'mobile'}><span aria-hidden="true">▯</span>手机版</button>
            </div>
            <button className="icon-button" onClick={onRefreshPreview} title="刷新预览" aria-label="刷新预览">↻</button>
            <button className="icon-button" onClick={onOpenExternal} disabled={!shownCode} title="新窗口打开" aria-label="新窗口打开">↗</button>
            {!isPublic && <button className="icon-button" onClick={onDownload} disabled={!shownCode} title="下载 HTML" aria-label="下载 HTML">↓</button>}
          </div>
        </div>
        {generating && <div className="mini-progress"><i style={{ width: `${Math.max(10, ((stepIndex + 1) / stages.length) * 100)}%` }} /></div>}
        {(isPublic || previewTab === 'app') ? <div className={`preview-canvas ${device === 'mobile' ? 'device-mobile' : ''}`}>
          {shownCode ? <iframe ref={previewFrameRef} key={`${device}-${currentSessionId || 'new'}-${previewRevision}`} title={`${sessionTitle || '应用'}预览`} sandbox="allow-scripts allow-forms allow-modals allow-popups allow-same-origin" srcDoc={previewSrc} /> : <div className="preview-empty"><div className="preview-empty-icon">◈</div><h2>实时预览</h2><p>开始描述需求后，生成的应用会显示在这里。</p><span>等待你的灵感输入</span></div>}
          {device === 'mobile' && shownCode && <div className="device-caption">移动端预览{mobileCode ? '' : ' · 暂无独立移动版本，显示桌面版'}</div>}
        </div> : <div className="file-browser">
          <div className="file-list"><div className="file-path">项目文件</div>{files.map((file) => <button key={file.name} className={activeFile === file.name ? 'active' : ''} onClick={() => onLoadFile(file.name)}><span>‹/›</span>{file.name}<small>{formatSize(file.size)}</small></button>)}{generating && <div className="file-generating"><i className="pulse-dot" />正在更新文件…</div>}</div>
          <pre className="file-content">{fileContent || (activeFile ? '文件内容为空。' : '选择一个文件查看源码。')}</pre>
        </div>}
      </section>}
      </section>
    </main>
  );
}

function formatSize(bytes) {
  return bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KB`;
}

function coverGlyph(title) {
  const kind = coverKind(title);
  return ({ mortgage: '◉', kanban: '▦', profile: '◌', pomodoro: '◷', notes: '▤' })[kind];
}

export default function App() {
  const [location, navigate] = useLocation();
  const [user, setUser] = useState(() => {
    const token = localStorage.getItem('p2a_token');
    const username = localStorage.getItem('p2a_user');
    return token ? { username: username || '用户' } : null;
  });
  const userRef = useRef(user);
  userRef.current = user;
  const [showcase, setShowcase] = useState([]);
  const [showcaseLoading, setShowcaseLoading] = useState(true);
  const [showcaseError, setShowcaseError] = useState('');
  const [sessions, setSessions] = useState([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [sessionsError, setSessionsError] = useState('');
  const [prompt, setPrompt] = useState('');
  const [input, setInput] = useState('');
  const [currentSessionId, setCurrentSessionId] = useState(null);
  const activeSessionIdRef = useRef(null);
  const [sessionTitle, setSessionTitle] = useState('');
  const [sessionOwned, setSessionOwned] = useState(false);
  const [messages, setMessages] = useState([]);
  const [versions, setVersions] = useState([]);
  const [files, setFiles] = useState([]);
  const [activeFile, setActiveFile] = useState('');
  const [fileContent, setFileContent] = useState('');
  const [code, setCode] = useState('');
  const [mobileCode, setMobileCode] = useState('');
  const [device, setDevice] = useState('desktop');
  const [previewTab, setPreviewTab] = useState('app');
  const [workspaceTab, setWorkspaceTab] = useState('chat');
  const [appState, setAppState] = useState(null);
  const [syncState, setSyncState] = useState('synced');
  const [sessionIsPublic, setSessionIsPublic] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [stageIndex, setStageIndex] = useState(-1);
  const [busy, setBusy] = useState({ busy: false, session_id: null, title: '' });
  const [apiStatus, setApiStatus] = useState('loading');
  const [loginOpen, setLoginOpen] = useState(false);
  const [pendingAction, setPendingAction] = useState(null);
  const [toast, setToast] = useState('');
  const abortRef = useRef(null);
  const previewFrameRef = useRef(null);
  const [previewRevision, setPreviewRevision] = useState(0);
  const savedStateTimer = useRef(null);
  const path = location.path === '/dashboard' || location.path === '/app' ? location.path : '/';

  const showError = useCallback((message) => {
    setToast(message || '请求失败，请稍后重试');
    window.clearTimeout(showError.timer);
    showError.timer = window.setTimeout(() => setToast(''), 6000);
  }, []);

  const loadShowcase = useCallback(async () => {
    setShowcaseLoading(true);
    setShowcaseError('');
    try {
      const items = await api('/api/public/showcase');
      setShowcase(items.filter((item) => item.title !== '个人主页生成器'));
    } catch (error) {
      setShowcase([]);
      setShowcaseError(readError(error));
    } finally {
      setShowcaseLoading(false);
    }
  }, []);

  const loadSessions = useCallback(async () => {
    setSessionsLoading(true);
    setSessionsError('');
    try {
      setSessions(await api('/api/sessions'));
    } catch (error) {
      setSessionsError(readError(error));
      throw error;
    } finally {
      setSessionsLoading(false);
    }
  }, []);

  const refreshBusy = useCallback(async () => {
    try {
      const status = await api('/api/generation/status');
      setBusy((previous) => previous.busy === status.busy
        && previous.session_id === status.session_id
        && previous.title === status.title ? previous : status);
      setApiStatus('online');
    } catch (error) {
      setApiStatus('offline');
    }
  }, []);

  const loadSession = useCallback(async (id, updateUrl = true, preserveWorkspaceTab = false) => {
    try {
      setSyncState('syncing');
      const detail = await api(`/api/sessions/${id}`);
      const activeUser = userRef.current;
      activeSessionIdRef.current = Number(detail.id);
      setCurrentSessionId(detail.id);
      setSessionTitle(detail.title || '未命名应用');
      setMessages(filterConversationMessages(detail.messages));
      setVersions(detail.versions || []);
      setCode(detail.latest_code || '');
      setMobileCode('');
      setSessionIsPublic(Number(detail.is_public) === 1);
      setSessionOwned(Boolean(activeUser) && Number(detail.is_public) === 0);
      setActiveFile('');
      setFileContent('');
      setAppState(null);
      if (activeUser && Number(detail.is_public) === 0) {
        const state = await api(`/api/sessions/${id}/state`);
        setAppState(state.state);
      }
      const fileList = await api(`/api/sessions/${id}/files`);
      setFiles(fileList.files || []);
      if ((fileList.files || []).some((file) => file.name === 'index_mobile.html')) {
        const mobile = await api(`/api/sessions/${id}/files/index_mobile.html`);
        setMobileCode(mobile.content || '');
      }
      if (updateUrl) navigate(`/app?session=${id}`);
      setPreviewTab('app');
      setDevice('desktop');
      if (!preserveWorkspaceTab) setWorkspaceTab(Number(detail.is_public) === 1 ? 'preview' : 'chat');
      setSyncState('synced');
      setToast('');
    } catch (error) {
      setSyncState('synced');
      showError(readError(error));
      if (!userRef.current) navigate('/');
    }
  }, [navigate, showError]);

  const startNew = useCallback(() => {
    activeSessionIdRef.current = null;
    setCurrentSessionId(null);
    setSessionTitle('');
    setSessionOwned(Boolean(user));
    setSessionIsPublic(false);
    setMessages([]);
    setVersions([]);
    setFiles([]);
    setCode('');
    setMobileCode('');
    setAppState(null);
    setActiveFile('');
    setFileContent('');
    setInput('');
    setPreviewTab('app');
    setWorkspaceTab('chat');
    navigate('/app?new=1');
  }, [navigate, user]);

  const openSession = useCallback((id) => {
    setInput('');
    navigate(`/app?session=${id}`);
  }, [navigate]);

  const stageFor = (content = '') => {
    if (content.includes('PM Agent')) return 0;
    if (content.includes('DesignSpec')) return 1;
    if (content.includes('移动端')) return 4;
    if (content.includes('已生成') && content.includes('字符')) return -1;
    if (content.includes('Chat Host')) return 6;
    if (content.includes('Tester')) return 5;
    if (content.includes('Reviewer')) return 3;
    if (content.includes('Coder Agent') || content.includes('正在编写代码') || content.includes('Coder 已按测试反馈')) return 2;
    return -1;
  };

  const send = useCallback(async (text = input, sessionIdOverride = undefined, authorized = false) => {
    const message = text.trim();
    if (!message || generating) return;
    if (!user && !authorized) {
      setPendingAction({ type: currentSessionId ? 'send' : 'create', text: message });
      setLoginOpen(true);
      return;
    }
    if (busy.busy && Number(busy.session_id) !== Number(currentSessionId)) {
      showError(`「${busy.title || '另一个应用'}」正在生成中，请稍候再试。`);
      return;
    }
    const targetSession = sessionIdOverride === undefined ? currentSessionId : sessionIdOverride;
    setGenerating(true);
    setStageIndex(0);
    setInput('');
    setPreviewTab('app');
    setWorkspaceTab('chat');
    setToast('');
    setMessages((previous) => [...previous, { id: `user-${Date.now()}`, role: 'user', content: message }]);
    abortRef.current = new AbortController();
    let completedSessionId = targetSession;
    let assistantMessageId = null;
    let previewOpened = false;
    const openPreview = () => {
      if (previewOpened) return;
      previewOpened = true;
      setWorkspaceTab('preview');
      setPreviewTab('app');
    };
    try {
      const response = await fetch('/api/chat', {
        method: 'POST',
        headers: { ...authHeaders(), 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: targetSession, message }),
        signal: abortRef.current.signal,
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail || `生成请求失败 (${response.status})`);
      }
      if (!response.body) throw new Error('服务器未返回生成数据流');
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      const handleChunk = (chunk) => {
        const data = chunk.split(/\r?\n/).filter((line) => line.startsWith('data:')).map((line) => line.slice(5).trimStart()).join('\n');
        if (!data) return;
        const event = JSON.parse(data);
        if (event.type === 'session' && event.session_id) {
          const sessionId = Number(event.session_id);
          completedSessionId = sessionId;
          activeSessionIdRef.current = sessionId;
          setCurrentSessionId(sessionId);
          if (event.title) setSessionTitle(event.title);
          setSessionOwned(true);
          setSessionIsPublic(false);
          navigate(`/app?session=${sessionId}`, true);
          if (sessionId !== Number(currentSessionId)) {
            loadSessions().catch((error) => showError(readError(error)));
          }
        } else if (event.type === 'status') {
          const content = event.content || '';
          const index = stageFor(content);
          if (index >= 0) setStageIndex(index);
          setMessages((previous) => [...previous, { id: `status-${Date.now()}-${Math.random()}`, role: 'status', content }]);
        } else if (event.type === 'text') {
          const content = event.content || '';
          if (isHtmlDocument(content)) {
            setCode(content);
            openPreview();
            return;
          }
          if (!assistantMessageId) assistantMessageId = `assistant-${Date.now()}`;
          const id = assistantMessageId;
          setMessages((previous) => {
            const existing = previous.some((item) => item.id === id);
            return existing
              ? previous.map((item) => item.id === id ? { ...item, content: item.content + content } : item)
              : [...previous, { id, role: 'assistant', content }];
          });
        } else if (event.type === 'code') {
          if (event.file === 'index_mobile.html') setMobileCode(event.content || '');
          else {
            setCode(event.content || '');
            openPreview();
          }
        } else if (event.type === 'done' && event.session_id) {
          completedSessionId = event.session_id;
          activeSessionIdRef.current = Number(event.session_id);
          setCurrentSessionId(event.session_id);
          navigate(`/app?session=${event.session_id}`, true);
        }
      };
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const chunks = buffer.split(/\r?\n\r?\n/);
        buffer = chunks.pop() || '';
        for (const chunk of chunks) {
          try { handleChunk(chunk); } catch (error) { showError(`生成事件解析失败：${readError(error)}`); }
        }
      }
      buffer += decoder.decode();
      if (buffer.trim()) {
        try { handleChunk(buffer); } catch (error) { showError(`生成事件解析失败：${readError(error)}`); }
      }
    } catch (error) {
      if (error.name !== 'AbortError') {
        showError(readError(error));
        setMessages((previous) => [...previous, { id: `error-${Date.now()}`, role: 'assistant', content: `生成失败：${readError(error)}` }]);
      }
    } finally {
      setGenerating(false);
      setStageIndex(-1);
      abortRef.current = null;
      await refreshBusy();
      if (completedSessionId) await loadSession(completedSessionId, false, previewOpened);
      if (user) {
        try { await loadSessions(); } catch { /* The error is shown by the project list surface. */ }
      }
    }
  }, [busy, currentSessionId, generating, input, loadSession, loadSessions, navigate, refreshBusy, showError, user]);

  const createFromPrompt = useCallback((text = prompt) => {
    const trimmed = text.trim();
    if (trimmed.length < 4) {
      showError('请至少输入 4 个字符，描述你想创建的应用。');
      return;
    }
    if (busy.busy) {
      showError(`「${busy.title || '另一个应用'}」正在生成中，请稍候再试。`);
      return;
    }
    if (!user) {
      setPendingAction({ type: 'create', text: trimmed });
      setLoginOpen(true);
      return;
    }
    setPrompt('');
    setCurrentSessionId(null);
    setSessionTitle(trimmed.length > 20 ? `${trimmed.slice(0, 20)}…` : trimmed);
    setSessionIsPublic(false);
    setWorkspaceTab('chat');
    setMessages([]);
    setCode('');
    setMobileCode('');
    setVersions([]);
    setFiles([]);
    setInput('');
    navigate('/app?new=1');
    send(trimmed, null);
  }, [busy, navigate, prompt, send, showError, user]);

  const requestLogin = useCallback((action = null) => {
    if (action) setPendingAction(action);
    setLoginOpen(true);
  }, []);

  const handleLogin = useCallback(async (username, password) => {
    const result = await api('/api/login', {
      method: 'POST',
      body: JSON.stringify({ username: username.trim(), password }),
    });
    localStorage.setItem('p2a_token', result.token);
    localStorage.setItem('p2a_user', result.username);
    setUser({ username: result.username });
    setLoginOpen(false);
    const action = pendingAction;
    setPendingAction(null);
    try { await loadSessions(); } catch { /* The dashboard displays this request error. */ }
    if (action?.type === 'create') {
      navigate('/app?new=1');
      setPrompt('');
      setCurrentSessionId(null);
      setSessionTitle(action.text.length > 20 ? `${action.text.slice(0, 20)}…` : action.text);
      setSessionIsPublic(false);
      setWorkspaceTab('chat');
      setMessages([]);
      setCode('');
      setMobileCode('');
      setVersions([]);
      setFiles([]);
      await send(action.text, null, true);
    } else if (action?.type === 'send') {
      await send(action.text, undefined, true);
    } else if (action?.type === 'dashboard') {
      navigate('/dashboard#mine');
    } else if (action?.type === 'new') {
      startNew();
    } else if (action?.type === 'prompt') {
      navigate('/');
    } else {
      navigate('/dashboard');
    }
  }, [loadSessions, navigate, pendingAction, send, startNew]);

  const logout = useCallback(() => {
    localStorage.removeItem('p2a_token');
    localStorage.removeItem('p2a_user');
    setUser(null);
    setSessions([]);
    if (path === '/dashboard') navigate('/');
    else if (path === '/app') {
      setSessionOwned(false);
      setSessionIsPublic(false);
      setCurrentSessionId(null);
      navigate('/');
    }
  }, [navigate, path]);

  const deleteSession = useCallback(async (id) => {
    const session = sessions.find((item) => Number(item.id) === Number(id));
    if (!window.confirm(`确定删除「${session?.title || '这个项目'}」吗？此操作无法撤销。`)) return;
    try {
      await api(`/api/sessions/${id}`, { method: 'DELETE' });
      await loadSessions();
      if (Number(id) === Number(currentSessionId)) startNew();
    } catch (error) {
      showError(readError(error));
    }
  }, [currentSessionId, loadSessions, sessions, showError, startNew]);

  const loadFile = useCallback(async (name) => {
    if (!currentSessionId) return;
    try {
      const file = await api(`/api/sessions/${currentSessionId}/files/${encodeURIComponent(name)}`);
      setActiveFile(file.name);
      setFileContent(file.content);
    } catch (error) {
      showError(readError(error));
    }
  }, [currentSessionId, showError]);

  const restoreVersion = useCallback(async (versionId) => {
    if (!versionId || !currentSessionId || !sessionOwned) return;
    try {
      await api(`/api/sessions/${currentSessionId}/versions/${versionId}/restore`, { method: 'POST' });
      await loadSession(currentSessionId, false);
    } catch (error) {
      showError(readError(error));
    }
  }, [currentSessionId, loadSession, sessionOwned, showError]);

  const downloadCode = useCallback(() => {
    const selected = device === 'mobile' && mobileCode ? mobileCode : code;
    if (!selected) return;
    const url = URL.createObjectURL(new Blob([selected], { type: 'text/html' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = `${sessionTitle || 'app'}${device === 'mobile' && mobileCode ? '_mobile' : ''}.html`;
    link.click();
    URL.revokeObjectURL(url);
  }, [code, device, mobileCode, sessionTitle]);

  const openExternal = useCallback(() => {
    const selected = device === 'mobile' && mobileCode ? mobileCode : code;
    if (selected) window.open(URL.createObjectURL(new Blob([selected], { type: 'text/html' })), '_blank', 'noopener');
  }, [code, device, mobileCode]);

  const loadDashboard = useCallback(async () => {
    try { await loadSessions(); } catch (error) { showError(readError(error)); }
  }, [loadSessions, showError]);

  useEffect(() => {
    let alive = true;
    api('/api/health').then(() => { if (alive) setApiStatus('online'); }).catch(() => { if (alive) setApiStatus('offline'); });
    loadShowcase();
    if (localStorage.getItem('p2a_token')) {
      loadSessions().catch((error) => {
        if (error.message.includes('401')) {
          localStorage.removeItem('p2a_token');
          localStorage.removeItem('p2a_user');
          setUser(null);
          showError('登录状态已失效，请重新登录。');
        } else showError(readError(error));
      });
    }
    refreshBusy();
    const timer = window.setInterval(refreshBusy, 5000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [loadShowcase, loadSessions, refreshBusy, showError]);

  useEffect(() => {
    if (location.path === '/app') {
      const query = new URLSearchParams(location.search);
      const id = Number(query.get('session'));
      const message = query.get('message');
      if (id && Number(currentSessionId) !== id && Number(activeSessionIdRef.current) !== id) loadSession(id, false);
      else if (message) {
        if (user) {
          send(message, null);
          navigate('/app', true);
        } else {
          requestLogin({ type: 'create', text: message });
          navigate('/', true);
        }
      } else if (query.get('new') === '1' && !user) requestLogin({ type: 'new' });
      else if (!user && !id) navigate('/');
    } else if (location.path === '/dashboard' && user) {
      loadDashboard();
    } else if (location.path === '/' && window.location.hash === '#showcase') {
      window.setTimeout(() => document.getElementById('showcase')?.scrollIntoView({ behavior: 'smooth' }), 50);
    }
  }, [location.path, location.search, location.hash, user, currentSessionId, loadSession, loadDashboard, navigate, requestLogin, send]);

  useEffect(() => {
    if (location.path !== '/dashboard' || location.hash !== '#mine' || !user) return undefined;
    const frame = window.requestAnimationFrame(() => {
      document.getElementById('mine')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [location.path, location.hash, user]);

  useEffect(() => {
    const listener = (event) => {
      const frame = previewFrameRef.current;
      if (event.source !== frame?.contentWindow || event.origin !== window.location.origin || event.data?.type !== 'p2a:state' || !event.data.state || typeof event.data.state !== 'object' || !currentSessionId || !user || !sessionOwned) return;
      setAppState(event.data.state);
    };
    window.addEventListener('message', listener);
    return () => window.removeEventListener('message', listener);
  }, [currentSessionId, sessionOwned, user]);

  useEffect(() => {
    if (!appState || !currentSessionId || !user || !sessionOwned) return undefined;
    setSyncState('syncing');
    window.clearTimeout(savedStateTimer.current);
    savedStateTimer.current = window.setTimeout(async () => {
      try {
        await api(`/api/sessions/${currentSessionId}/state`, { method: 'PUT', body: JSON.stringify({ state: appState }) });
        setSyncState('synced');
      } catch (error) {
        setSyncState('synced');
        showError(`应用状态同步失败：${readError(error)}`);
      }
    }, 1000);
    return () => window.clearTimeout(savedStateTimer.current);
  }, [appState, currentSessionId, sessionOwned, showError, user]);

  const openPublic = useCallback((id) => openSession(id), [openSession]);
  const useExample = useCallback((text) => {
    setPrompt(text);
    navigate('/');
    window.setTimeout(() => {
      const field = document.getElementById('prompt-input');
      field?.scrollIntoView({ behavior: 'smooth', block: 'center' });
      field?.focus();
      field?.setSelectionRange(text.length, text.length);
    }, 60);
  }, [navigate]);
  const newAction = useCallback(() => {
    if (!user) requestLogin({ type: 'new' });
    else startNew();
  }, [requestLogin, startNew, user]);
  const dashboardAction = useCallback(() => {
    if (!user) {
      requestLogin({ type: 'dashboard' });
      return;
    }
    navigate('/dashboard#mine');
  }, [navigate, requestLogin, user]);

  return (
    <div className="app-shell">
      <Header path={path} user={user} syncState={path === '/app' ? syncState : ''} onNavigate={navigate} onProjects={dashboardAction} onLogin={() => requestLogin()} onLogout={logout} />
      {toast && <div className="toast" role="alert"><span>{toast}</span><button onClick={() => setToast('')} aria-label="关闭错误提示">×</button></div>}
      {path === '/' && <HomePage user={user} showcase={showcase} showcaseLoading={showcaseLoading} showcaseError={showcaseError} prompt={prompt} setPrompt={setPrompt} busy={busy} onCreate={() => createFromPrompt()} onUsePrompt={useExample} onOpen={openPublic} onRetry={loadShowcase} onLogin={() => requestLogin({ type: 'new' })} apiStatus={apiStatus} />}
      {path === '/dashboard' && <DashboardPage user={user} showcase={showcase} sessions={sessions} loading={sessionsLoading} error={sessionsError} onRefresh={loadDashboard} onOpen={openSession} onDelete={deleteSession} onLogin={() => requestLogin({ type: 'dashboard' })} onCreate={(text) => typeof text === 'string' && text ? useExample(text) : newAction()} />}
      {path === '/app' && <Workbench
        user={user} sessionTitle={sessionTitle} currentSessionId={currentSessionId} sessionOwned={sessionOwned} isPublic={sessionIsPublic}
        sessions={sessions.length ? sessions : showcase.map((item) => ({ id: item.id, title: item.title }))}
        messages={messages} input={input} setInput={setInput} generating={generating} busy={busy} stepIndex={stageIndex}
        workspaceTab={workspaceTab} setWorkspaceTab={setWorkspaceTab}
        code={code} mobileCode={mobileCode} device={device} setDevice={setDevice} previewTab={previewTab} setPreviewTab={setPreviewTab}
        files={files} activeFile={activeFile} fileContent={fileContent} versions={versions}
        onOpenSession={openSession} onNewSession={newAction} onSend={() => send()} onStop={() => abortRef.current?.abort()}
        onLogin={() => requestLogin({ type: 'send', text: input })} onLoadFile={loadFile} onRestoreVersion={restoreVersion}
        onDownload={downloadCode} onOpenExternal={openExternal}
        syncState={syncState} previewFrameRef={previewFrameRef} previewRevision={previewRevision} appState={appState}
        onRefreshPreview={() => setPreviewRevision((revision) => revision + 1)}
      />}
      {loginOpen && <LoginModal onClose={() => {
        const action = pendingAction;
        setLoginOpen(false);
        setPendingAction(null);
        if (!user && ['create', 'new', 'send'].includes(action?.type)) navigate('/');
      }} onSubmit={handleLogin} />}
    </div>
  );
}
