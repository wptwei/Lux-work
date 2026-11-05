import React, { useMemo, useState } from "react";
import {
  Activity, Bot, BrainCircuit, Check, ChevronDown, CircleUserRound,
  FileDown, FileText, Gauge, LayoutDashboard, Mic, Paperclip, Send,
  ShieldCheck, Sparkles, UploadCloud, Workflow, X
} from "lucide-react";

const navItems = [
  [Sparkles, "智能助手"], [Activity, "运行中心"], [Gauge, "效能看板"],
  [Workflow, "事件处置"], [ShieldCheck, "人工审核台"],
];

const capabilities = [
  [BrainCircuit, "本地推理", "业务数据不出域"],
  [ShieldCheck, "红线守卫", "输入输出双向检测"],
  [FileText, "文档协作", "周报 · PPT · 邮件"],
  [Workflow, "处置闭环", "多 Agent 协同"],
];

const Glass = ({ className = "", children }) => (
  <section className={`relative overflow-hidden border border-white/10 bg-slate-900/40 shadow-2xl shadow-black/30 backdrop-blur-2xl ${className}`}>
    <div className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-white/30 via-white/5 to-transparent" />
    {children}
  </section>
);

export default function HcopilotAurora() {
  const [active, setActive] = useState("智能助手");
  const [skill, setSkill] = useState("[管理岗] H区周报（动态创作）");
  const [message, setMessage] = useState("");
  const [messages, setMessages] = useState([
    { role: "assistant", text: "晚上好。我是 H-Copilot，本地安全智能核心已就绪。你可以直接描述任务，或附加需要处理的文件。" },
    { role: "user", text: "帮我整理本周 H 区巡检情况，并输出管理层摘要。" },
    { role: "assistant", text: "已进入管理岗周报流程。我会先完成红线检查，再按风险、进展和下周计划三个层级组织内容。" },
  ]);

  const canSend = useMemo(() => message.trim().length > 0, [message]);
  const send = () => {
    if (!canSend) return;
    setMessages((items) => [...items, { role: "user", text: message.trim() }]);
    setMessage("");
  };

  return (
    <main className="relative min-h-screen overflow-hidden bg-[#050505] font-[Inter] text-white selection:bg-cyan-400/25">
      <div className="pointer-events-none fixed inset-0">
        <div className="absolute -left-48 -top-56 h-[620px] w-[620px] rounded-full bg-emerald-500/20 blur-[150px]" />
        <div className="absolute left-[34%] top-[12%] h-[720px] w-[720px] rounded-full bg-indigo-600/20 blur-[180px]" />
        <div className="absolute -right-40 bottom-[-260px] h-[700px] w-[700px] rounded-full bg-cyan-500/20 blur-[170px]" />
        <div className="absolute inset-0 opacity-[0.035] [background-image:linear-gradient(rgba(255,255,255,.3)_1px,transparent_1px),linear-gradient(90deg,rgba(255,255,255,.3)_1px,transparent_1px)] [background-size:48px_48px]" />
      </div>

      <div className="relative grid min-h-screen grid-cols-[220px_minmax(0,1fr)_310px] gap-4 p-5">
        <Glass className="flex flex-col rounded-[28px] p-3">
          <div className="flex items-center gap-3 px-3 py-4">
            <div className="grid h-11 w-11 place-items-center rounded-2xl bg-gradient-to-br from-cyan-400/30 to-indigo-500/30 shadow-[0_0_35px_rgba(34,211,238,.2)]">
              <ShieldCheck className="h-6 w-6 text-cyan-200" />
            </div>
            <div><p className="text-sm font-semibold tracking-[.14em]">H-COPILOT</p><p className="text-[10px] text-slate-400">SECURITY INTELLIGENCE</p></div>
          </div>
          <nav className="mt-6 space-y-1.5">
            {navItems.map(([Icon, label]) => (
              <button key={label} onClick={() => setActive(label)} className={`group flex w-full items-center gap-3 rounded-2xl px-3 py-3 text-left text-sm transition ${active === label ? "bg-gradient-to-r from-blue-500/20 to-cyan-400/5 text-white shadow-[inset_2px_0_0_#22d3ee,0_0_25px_rgba(59,130,246,.08)]" : "text-slate-400 hover:bg-white/5 hover:text-white"}`}>
                <Icon className={`h-4 w-4 ${active === label ? "text-cyan-300" : "group-hover:text-cyan-300"}`} />{label}
              </button>
            ))}
          </nav>
          <div className="mt-auto rounded-2xl border border-emerald-400/15 bg-emerald-400/[.05] p-3">
            <div className="flex items-center gap-2 text-[11px] font-medium text-emerald-200"><span className="h-2 w-2 animate-pulse rounded-full bg-emerald-400 shadow-[0_0_18px_#10b981]" />LOCAL CORE ACTIVE</div>
            <p className="mt-2 text-[10px] leading-5 text-slate-500">OLLAMA / QWEN2.5<br />100% 本地离线</p>
          </div>
        </Glass>

        <div className="relative flex min-w-0 flex-col gap-4 pb-24">
          <Glass className="grid grid-cols-4 gap-2 rounded-[22px] p-2">
            {capabilities.map(([Icon, title, subtitle]) => <div key={title} className="flex items-center gap-2 rounded-2xl px-3 py-2 hover:bg-white/[.04]"><Icon className="h-4 w-4 text-cyan-300" /><div><p className="text-xs font-medium">{title}</p><p className="text-[9px] text-slate-500">{subtitle}</p></div></div>)}
          </Glass>

          <Glass className="flex min-h-0 flex-1 flex-col rounded-[30px]">
            <header className="flex items-center justify-between px-7 py-5"><div><p className="text-[10px] tracking-[.2em] text-cyan-300">AI WORKSPACE</p><h1 className="mt-1 text-xl font-semibold">安全智能协作空间</h1></div><span className="rounded-full border border-emerald-300/15 bg-emerald-400/[.06] px-3 py-1.5 text-[10px] text-emerald-200">● AIR-GAPPED</span></header>
            <div className="mx-7 h-px bg-gradient-to-r from-white/10 to-transparent" />
            <div className="flex-1 space-y-7 overflow-y-auto px-7 py-7 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
              {messages.map((item, index) => item.role === "assistant" ? (
                <div key={index} className="flex max-w-[88%] gap-3"><div className="mt-1 grid h-8 w-8 shrink-0 place-items-center rounded-xl bg-cyan-400/10 text-cyan-200"><Bot className="h-4 w-4" /></div><div className="border-l border-cyan-300/40 pl-4 text-sm leading-7 text-slate-200">{item.text}</div></div>
              ) : (
                <div key={index} className="ml-auto max-w-[72%] rounded-[22px] rounded-br-md bg-gradient-to-br from-blue-600/25 to-cyan-500/10 px-5 py-3 text-sm leading-6 text-blue-50 shadow-[0_12px_40px_rgba(37,99,235,.08)]">{item.text}</div>
              ))}
            </div>
          </Glass>

          <Glass className="absolute inset-x-[5%] bottom-1 rounded-full p-2 shadow-[0_25px_80px_rgba(0,0,0,.65),0_0_45px_rgba(79,70,229,.13)]">
            <div className="flex items-center gap-2"><button className="grid h-10 w-10 place-items-center rounded-full text-slate-400 hover:bg-white/5 hover:text-cyan-200"><Paperclip className="h-5 w-5" /></button><input value={message} onChange={(e) => setMessage(e.target.value)} onKeyDown={(e) => e.key === "Enter" && send()} placeholder="输入任务或粘贴工作素材…" className="min-w-0 flex-1 bg-transparent px-2 text-sm text-white outline-none placeholder:text-slate-600" /><button className="grid h-10 w-10 place-items-center rounded-full text-slate-500 hover:bg-white/5 hover:text-cyan-200"><Mic className="h-5 w-5" /></button><button onClick={send} disabled={!canSend} className="grid h-11 w-11 place-items-center rounded-full bg-gradient-to-br from-blue-500 to-violet-500 shadow-[0_0_28px_rgba(99,102,241,.45)] transition hover:scale-105 disabled:opacity-35"><Send className="h-4 w-4" /></button></div>
          </Glass>
        </div>

        <aside className="space-y-4">
          <Glass className="rounded-[24px] p-5"><p className="font-mono text-[9px] tracking-[.18em] text-cyan-300">ROLE PLAYBOOK</p><h2 className="mt-2 text-base font-semibold">岗位专项技能</h2><div className="relative mt-4"><select value={skill} onChange={(e) => setSkill(e.target.value)} className="w-full appearance-none rounded-2xl border border-white/10 bg-white/[.04] px-4 py-3 pr-10 text-xs text-slate-200 outline-none focus:border-cyan-400/40"><option>[管理岗] H区周报（动态创作）</option><option>[门禁岗] 异常事件说明</option><option>[巡逻岗] 巡检记录整理</option></select><ChevronDown className="pointer-events-none absolute right-3 top-3.5 h-4 w-4 text-slate-500" /></div><button className="mt-3 w-full rounded-2xl bg-gradient-to-r from-blue-500/80 to-violet-500/80 py-2.5 text-xs font-medium shadow-[0_0_25px_rgba(99,102,241,.18)]">启用技能</button></Glass>

          <Glass className="rounded-[24px] p-5"><p className="font-mono text-[9px] tracking-[.18em] text-violet-300">DELIVERABLES</p><h2 className="mt-2 text-base font-semibold">交付导出</h2><div className="mt-4 grid grid-cols-3 rounded-2xl bg-black/20 p-1 text-[10px]"><button className="rounded-xl bg-white/10 py-2 text-white">标准版</button><button className="py-2 text-slate-500">套用模板</button><button className="py-2 text-slate-500">保留底版</button></div><label className="mt-4 flex cursor-pointer flex-col items-center rounded-2xl border border-dashed border-cyan-300/25 bg-cyan-400/[.025] px-4 py-6 text-center transition hover:border-cyan-300/50 hover:bg-cyan-400/[.05]"><UploadCloud className="h-6 w-6 text-cyan-300" /><span className="mt-2 text-xs text-slate-300">拖放 PPT 模板</span><span className="mt-1 text-[10px] text-slate-600">或点击选择本地文件</span><input type="file" className="hidden" /></label></Glass>

          <Glass className="rounded-[24px] p-5"><p className="font-mono text-[9px] tracking-[.18em] text-emerald-300">SECURITY STATUS</p><div className="mt-4 space-y-3">{["本地附件识别与图片分类", "敏感信息双向拦截与脱敏", "人工复核与操作留痕"].map((item) => <div key={item} className="flex items-center gap-2 text-[11px] text-slate-400"><span className="grid h-5 w-5 place-items-center rounded-full bg-emerald-400/10"><Check className="h-3 w-3 text-emerald-300" /></span>{item}</div>)}</div></Glass>
        </aside>
      </div>
    </main>
  );
}
