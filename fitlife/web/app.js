"use strict";
const $ = id => document.getElementById(id);
const days = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
const names = {temporary_constraints:"临时约束",height_cm:"身高 cm",age:"填写时年龄",sex:"性别",space:"训练空间",equipment:"器械",limitations:"训练限制",diet_preferences:"饮食偏好",walking:"步行",gentle_mobility:"轻柔关节活动",goal:"运动目标",preferred_activities:"偏好活动",available_weekdays:"可用星期",weekly_sessions:"每周次数",session_minutes:"每次分钟"};
const statuses = {pending:"待确认，尚未保存",applied:"已保存",rejected:"已拒绝",expired:"已过期",superseded:"已失效，请重新提出要求"};
const errors = {STALE_CONVERSATION_TITLE:"对话名称已变化，请重新打开重命名后保存。",INVALID_CONVERSATION_TITLE:"名称需为1至40字，不能含换行或控制字符。",UNAUTHENTICATED:"连接已失效，请重新输入个人令牌。",STALE_CONFIRMATION:"内容已变更，请刷新后检查新草案。",ACTION_NOT_PENDING:"该草案已处理或失效，请刷新。",REQUEST_IN_PROGRESS:"刚才的请求仍在处理，请稍后用原请求重试。",CONVERSATION_IN_PROGRESS:"这段对话正在处理请求，请稍后再试。",AGENT_CAPACITY_REACHED:"当前较忙，请稍后重试。",INVALID_INPUT:"请检查填写内容、日期或数值范围。",INVALID_WEEK_START:"请将周起点选为周一。",PROFILE_SCHEDULE_CONFLICT:"每周次数不能超过可用天数。",DATABASE_UNAVAILABLE:"暂时无法连接数据服务，请稍后重试。",AGENT_FAILED:"本次请求未完成，可以重试原请求找回结果。"};
let token = "", conversation = null, failedRequest = null, busy = false, titleRevision = 0;
const dateText = date => new Intl.DateTimeFormat("en-CA",{timeZone:"Asia/Shanghai",year:"numeric",month:"2-digit",day:"2-digit"}).format(date);
const today = dateText(new Date());
const now = new Date(`${today}T12:00:00Z`); now.setUTCDate(now.getUTCDate() - (now.getUTCDay()+6)%7);
$("week").value = dateText(now); $("activity-day").value = $("start").value = $("end").value = today;
$("activity-day").max = today;
function element(tag, text, cls){const node=document.createElement(tag);if(text!==undefined)node.textContent=String(text);if(cls)node.className=cls;return node;}
function notice(text=""){$("status").textContent=text;}
async function api(path,method="GET",body,key){
  const headers={Authorization:`Bearer ${token}`};if(body!==undefined)headers["Content-Type"]="application/json";if(key)headers["Idempotency-Key"]=key;
  const response=await fetch(path,{method,headers,body:body===undefined?undefined:JSON.stringify(body),cache:"no-store"});
  const data=response.status===204?{}:await response.json();
  if(!response.ok){const code=data.error_code||data.detail?.error_code||data.detail||"REQUEST_FAILED";throw new Error(errors[code]||"操作未完成，请检查连接和输入后重试。");}return data;
}
async function action(button,fn){if(busy)return;busy=true;const controls=[...document.querySelectorAll("button,input,select,textarea")].map(node=>[node,node.disabled]);controls.forEach(([node])=>node.disabled=true);notice();try{await fn();}catch(error){notice(error.message||"操作失败，请重试。");}finally{controls.forEach(([node,disabled])=>node.disabled=disabled);busy=false;}}
function describe(key,value){if(key==="temporary_constraints")return value.map(c=>`${c.name}（${c.start_date} 至 ${c.end_date}）：${c.note}`).join("；")||"暂无";if(key==="available_weekdays")return value.map(i=>days[i]).join("、");if(Array.isArray(value))return value.map(v=>names[v]||v).join("、");return names[value]||value;}
function renderOperation(kind,payload,parent){
  const labels={body:"身体测量",steps:"步数",workout:"训练记录",food:"食物资料",meal:"餐食",photo_meal:"照片估算餐食"};
  if(kind==="record_activity"||kind==="delete_activity"){parent.append(element("h3","活动记录"),element("p",`${payload.occurred_on} · ${names[payload.activity_id]} · ${payload.minutes} 分钟`));return;}
  if(kind==="adjust_plan"){
    const e=payload.adjustment;
    const progress=e.reference_summary.planned_sessions?`参考周计划 ${e.reference_summary.planned_sessions} 项，按已有记录匹配完成 ${e.reference_summary.completed_sessions} 项。`:"参考周暂无已保存计划，无法计算计划完成率。";
    parent.append(element("h3","计划调整：请核对前后变化"),element("p",e.explanation),element("p",`参考周：${e.reference_week_start}；${progress} 未记录不代表未运动。`));
    const show=s=>s?`${names[s.activity_id]||s.activity_id} ${s.minutes} 分钟`:"无安排";
    e.changes.forEach(c=>parent.append(element("p",`${c.day}：${show(c.before)} → ${show(c.after)}`)));
    parent.append(element("p",`保留过去及已有记录安排 ${e.locked_sessions.length} 项；确认前会再次核对记录。`));
    const details=element("details");details.append(element("summary","查看依据"),element("p",`原计划版本 ${payload.source_plan_version}；画像版本 ${payload.source_profile_version}；活动记录 ${e.activity_record_ids.length} 条；依据摘要 ${e.evidence_digest.slice(0,12)}。`));parent.append(details);return;
  }
  if(kind==="save_plan"){parent.append(element("h3","周计划"));if(payload.temporary_constraint_exceptions?.length)parent.append(element("p",`本次明确例外：${payload.temporary_constraint_exceptions.join("、")}`));payload.sessions.forEach(s=>parent.append(element("p",`${s.day} · ${names[s.activity_id]} · ${s.minutes} 分钟`)));return;}
  if(kind==="delete_domain_record"){parent.append(element("h3","删除记录"));renderOperation("domain_record",payload,parent);parent.append(element("p","确认后删除上面这条记录，已核算的历史餐食快照保留。"));return;}
  if(kind!=="domain_record"){parent.append(element("h3","画像变更"));Object.entries(payload).forEach(([k,v])=>parent.append(element("p",`${names[k]||k}：${describe(k,v)}`)));return;}
  const fields={weight_kg:"体重 kg",body_fat_pct:"体脂率 %",waist_cm:"腰围 cm",sleep_hours:"睡眠小时",fatigue:"疲劳描述",record_date:"日期",steps:"步数",name:"名称",unit:"单位",energy_kcal:"每100单位热量 kcal",protein_g:"每100单位蛋白质 g",carbs_g:"每100单位碳水 g",fat_g:"每100单位脂肪 g",source:"来源",meal_date:"餐食日期",meal_type:"餐别",feedback:"反馈"};
  parent.append(element("h3",labels[payload.record_type]||"记录"));
  const values=payload.values;
  Object.entries(values).forEach(([key,value])=>{
    if(value===null)return;
    if(key==="items"){
      const items=values.calculation?.items||value;
      items.forEach(item=>parent.append(element("p",[item.name||"已确认食物",item.category,item.amount?`${item.amount} ${item.unit}`:item.amount_g?`估计 ${item.amount_g} g`:null,item.duration_min?`${item.duration_min} 分钟`:null,item.sets?`${item.sets} 组`:null,item.reps?`次数 ${item.reps}`:null,item.resistance,item.feedback,item.estimated_energy_kcal!==undefined?`估计热量 ${item.estimated_energy_kcal??"未知"} kcal`:null].filter(Boolean).join(" · "))));return;
    }
    if(key==="calculation"){const total=value.total;parent.append(element("p",`按食物资料核算：${total.energy_kcal} kcal；蛋白质 ${total.protein_g} g；碳水 ${total.carbs_g} g；脂肪 ${total.fat_g} g。`));return;}
    const shown=value==="vision_estimate_user_confirmed"?"照片估算，经本人确认":value;
    parent.append(element("p",`${fields[key]||names[key]||key}：${shown}`));
  });
}

function candidate(data,parent){
  parent.replaceChildren();const box=element("div",undefined,"candidate");box.dataset.status=data.status;parent.append(box);
  const title={action_bundle:"请检查整份变更",domain_record:"请检查这条记录",delete_domain_record:"请检查要删除的记录",record_activity:"请检查这次活动",delete_activity:"请检查要删除的活动",profile_patch:"请检查偏好变更",profile_forget:"请检查要忘记的信息",save_plan:"请检查周计划",adjust_plan:"请检查计划调整"};
  const resolved=data.status==="applied"?(["delete_activity","delete_domain_record"].includes(data.kind)?"已删除":data.kind==="profile_forget"?"已忘记":"已保存"):statuses[data.status]||data.status;
  box.append(element("h2",title[data.kind]||"请检查变更"),element("span",resolved,"badge"));
  const payload=data.payload;
  if(data.kind==="action_bundle"){
    box.append(element("p","本次包含多个领域的变更，确认后在同一事务保存；拒绝则全部不保存。"));
    payload.operations.forEach(op=>{const section=element("section");box.append(section);renderOperation(op.kind,op.payload,section);});
  }else if(["domain_record","adjust_plan"].includes(data.kind))renderOperation(data.kind,payload,box);
  else if(data.kind==="save_plan"){
    box.append(element("p",`${payload.week_start} 开始的一周 · 将保存为新版本`));
    if(payload.temporary_constraint_exceptions?.length)box.append(element("p",`本次明确例外：${payload.temporary_constraint_exceptions.join("、")}。确认后仅作用于本计划，不改已保存约束。`));
    if(payload.temporary_constraints_applied?.length)box.append(element("p",`适用临时约束：${payload.temporary_constraints_applied.join("、")}`));
    payload.sessions.forEach(s=>box.append(element("p",`${s.day} · ${names[s.activity_id]} · ${s.minutes} 分钟`)));
    const labels={activity_id:"活动项目",available_weekdays:"可用星期",weekly_sessions:"每周次数",session_minutes:"每次时长"};const overrides=(payload.explicit_overrides||[]).map(field=>labels[field]);
    box.append(element("small",`${overrides.length?"本次明确调整："+overrides.join("、")+"。":"按你已有的时间与活动偏好安排。"}未指定部分优先保留原计划；本周修改不会改写长期偏好。`));
    const basis=element("details");basis.append(element("summary","查看生成依据记录"),element("p",`画像版本 ${payload.source_profile_version}；原计划版本 ${payload.source_plan_version}；安排规则 ${payload.rule_version}。`));box.append(basis);
  }else if(["record_activity","delete_activity"].includes(data.kind))box.append(element("p",`${payload.occurred_on} · ${names[payload.activity_id]} · ${payload.minutes} 分钟${data.kind==="delete_activity"?" · 确认后从活动中删除，进度会重新计算。":""}`));
  else if(data.kind==="profile_forget")box.append(element("p",`忘记：${payload.fields.map(f=>names[f]).join("、")}。已有活动和计划保留。`));
  else Object.entries(payload).forEach(([k,v])=>box.append(element("p",`${names[k]||k}：${describe(k,v)}`)));
  if(data.status==="pending"){
    const controls=element("div",undefined,"actions");box.append(controls);
    for(const [operation,label] of [["confirm",["delete_activity","delete_domain_record"].includes(data.kind)?"确认删除":data.kind==="profile_forget"?"确认忘记":"确认保存"],["reject","拒绝"]]){
      const button=element("button",label,operation==="reject"?"secondary":"");controls.append(button);
      button.onclick=()=>action(button,async()=>{const updated=await api(`/actions/${data.action_id}/${operation}`,"POST",{revision:data.revision,digest:data.digest});candidate(updated,parent);await refreshPanels();});
    }
    if(["record_activity","save_plan"].includes(data.kind)){
      const label=element("label","修改分钟数"),input=element("input");input.type="number";input.min="1";input.max=data.kind==="save_plan"?"180":"1440";input.value=data.kind==="save_plan"?payload.sessions[0].minutes:payload.minutes;label.append(input);controls.append(label);
      const edit=element("button","更新草案","secondary");controls.append(edit);
      edit.onclick=()=>action(edit,async()=>{if(!input.reportValidity())return;const modified=structuredClone(payload);if(data.kind==="save_plan")modified.sessions.forEach(s=>s.minutes=Number(input.value));else modified.minutes=Number(input.value);candidate(await api(`/actions/${data.action_id}`,"PATCH",{revision:data.revision,digest:data.digest,payload:modified}),parent);});
    }
    box.append(element("small",`有效期至 ${new Date(data.expires_at).toLocaleTimeString("zh-CN")}；修改后需重新确认。`));
  }
}
async function loadConversations(){
  const data=await api("/conversations");$("conversations").replaceChildren();
  data.conversations.forEach(item=>{const option=element("option",item.title||"新对话");option.value=item.conversation_id;option.title=`最近更新：${new Date(item.updated_at).toLocaleString("zh-CN")}`;$("conversations").append(option);});
  if(conversation)$("conversations").value=conversation;
}
async function newConversation(){const data=await api("/conversations","POST");conversation=data.conversation_id;failedRequest=null;$("retry").hidden=true;await loadConversations();await loadChat();}
async function loadChat(){
  $("messages").replaceChildren();if(!conversation)return;
  const data=await api(`/conversations/${conversation}`);
  titleRevision=data.title_revision;$("conversation-title").value=data.title||"新对话";$("conversation-title-form").hidden=true;
  if(!data.turns.length){const empty=element("div",undefined,"chat-empty");empty.append(element("span","↗","empty-mark"),element("h2","今天，从一件小事开始"),element("p","记录一次活动，安排接下来的一周，或回顾已经完成的事。"));$("messages").append(empty);}
  for(const turn of data.turns){
    if(turn.message)$("messages").append(element("div",turn.message,"bubble user"));
    const answer=turn.response?.answer||(turn.status==="running"?"请求仍在处理中，可用原请求重试。":"本次请求未完成。");
    const bubble=element("div",answer,"bubble assistant");$("messages").append(bubble);
    if(["failed","running"].includes(turn.status)&&turn.message){const retry=element("button","重试这条请求","secondary recover-request");retry.dataset.requestKey=turn.request_key;retry.onclick=()=>action(retry,()=>sendRequest({key:turn.request_key,message:turn.message,conversation}));bubble.append(retry);}
    if(turn.response?.pending_action){const slot=element("div");bubble.append(slot);candidate(turn.response.pending_action,slot);}
    if(turn.response?.run_id){const details=element("details");details.append(element("summary","执行凭据"),element("p",`运行：${turn.response.run_id} · 步数：${turn.response.steps}`));bubble.append(details);}
  }
  $("messages").scrollTop=$("messages").scrollHeight;
}
async function sendRequest(request){
  document.querySelector(".chat-empty")?.remove();
  failedRequest=request;$("retry").hidden=true;
  try{await api("/agent/requests","POST",{message:request.message,conversation_id:request.conversation},request.key);failedRequest=null;$("message").value="";await loadChat();await loadConversations();await refreshPanels();}
  catch(error){$("retry").hidden=false;await loadChat();throw error;}
}
async function loadPlan(){
  const week=$("week").value;if(!week)return;const progress=await api(`/plans/${week}/progress`);const parent=$("plan-content");parent.replaceChildren();
  if(progress.status==="no_plan"){parent.append(element("p","这一周还没有保存计划。在对话中告诉我你的安排，或先设置目标与偏好。"));return;}
  const metric=element("div",undefined,"metric");metric.append(element("strong",progress.completion_percent===null?"暂无安排目标":`${progress.completion_percent}%`),element("span",`按计划匹配 ${progress.matched_minutes} / ${progress.planned_minutes} 分钟`));parent.append(metric);
  progress.sessions.forEach(s=>{const row=element("div",undefined,"list-row");row.append(element("span",`${s.day} · ${names[s.activity_id]}`),element("span",`${s.recorded_minutes} / ${s.minutes} 分钟 · ${{completed:"已完成",partial:"部分完成",unrecorded:"未记录"}[s.status]}`));parent.append(row);});
  parent.append(element("p",`本周共记录 ${progress.recorded_minutes} 分钟；额外时长 ${progress.extra_recorded_minutes} 分钟。`),element("small",progress.note));
}
async function loadRecords(offset=0){
  const data=await api(`/activities?start_date=${$("start").value}&end_date=${$("end").value}&limit=100&offset=${offset}`);const parent=$("records");parent.replaceChildren(element("p",`共记录 ${data.record_count} 次 · ${data.recorded_minutes} 分钟`));
  data.records.forEach(record=>{const row=element("div",undefined,"list-row");row.append(element("span",`${record.occurred_on} · ${names[record.activity_id]}`),element("span",`${record.minutes} 分钟`));const remove=element("button","删除","quiet");remove.onclick=()=>action(remove,async()=>{candidate(await api(`/activities/${record.id}/deletion-actions`,"POST",undefined,crypto.randomUUID()),$("activity-candidate"));$("activity-candidate").scrollIntoView({behavior:"smooth"});});row.append(remove);parent.append(row);});
  if(offset>0){const previous=element("button","上一页","secondary");previous.onclick=()=>action(previous,()=>loadRecords(Math.max(0,offset-100)));parent.append(previous);}
  if(data.has_more){const next=element("button","下一页","secondary");next.onclick=()=>action(next,()=>loadRecords(offset+data.returned_count));parent.append(next);}
}
async function loadProfile(){
  const data=await api("/profile");const parent=$("memory");parent.replaceChildren();
  for(const [key,value] of Object.entries(data.values)){
    const row=element("div",undefined,"list-row"),text=element("div");text.append(element("p",`${names[key]}：${describe(key,value)}`),element("small",`你明确提供 · ${data.provenance[key]?.updated_at||""}`));row.append(text);
    const forget=element("button","忘记这一项","quiet");row.append(forget);forget.onclick=()=>action(forget,async()=>candidate(await api("/profile/forget-actions","POST",{fields:[key]},crypto.randomUUID()),$("profile-candidate")));parent.append(row);
  }
  if(!Object.keys(data.values).length)parent.append(element("p","尚未保存偏好。可以在下面填写，也可以在对话中说明。"));
  $("goal").value=data.values.goal||"";$("preference").value=data.values.preferred_activities?.[0]||"";$("frequency").value=data.values.weekly_sessions||"";$("duration").value=data.values.session_minutes||"";
  document.querySelectorAll("[name=weekday]").forEach(input=>input.checked=(data.values.available_weekdays||[]).includes(Number(input.value)));
}
async function refreshPanels(){await Promise.all([loadProfile(),loadPlan(),loadRecords()]);if(typeof loadMediaJobs==="function")await loadMediaJobs();}
$("login-form").onsubmit=event=>{event.preventDefault();action(event.submitter,async()=>{token=$("token").value.trim();try{await api("/me");await loadConversations();conversation=$("conversations").value||null;if(!conversation)await newConversation();else await loadChat();await refreshPanels();$("login").hidden=true;$("workspace").hidden=false;$("logout").hidden=false;$("token").value="";}catch(error){token="";throw error;}});};
$("logout").onclick=()=>action($("logout"),async()=>{let revokeError=null;try{await api("/auth/token","DELETE");}catch(error){revokeError=error;}finally{if(typeof clearMedia==="function")clearMedia();token="";conversation=null;failedRequest=null;titleRevision=0;$("conversation-title-form").hidden=true;$("workspace").hidden=true;$("login").hidden=false;$("logout").hidden=true;$("retry").hidden=true;["messages","profile-candidate","activity-candidate","records","memory","plan-content","conversations"].forEach(id=>$(id).replaceChildren());["token","goal","message","activity-minutes","frequency","duration","preference","conversation-title"].forEach(id=>$(id).value="");document.querySelectorAll("[name=weekday]").forEach(input=>input.checked=false);}if(revokeError)notice("已退出本页；访问凭证已失效或暂未能在服务端注销。");});
document.querySelectorAll("[data-tab]").forEach(button=>button.onclick=()=>{document.querySelectorAll(".tab").forEach(panel=>panel.hidden=panel.id!==button.dataset.tab);document.querySelectorAll("[data-tab]").forEach(b=>b.classList.toggle("selected",b===button));notice();});
$("new-chat").onclick=()=>action($("new-chat"),newConversation);
$("conversations").onchange=()=>action($("conversations"),async()=>{conversation=$("conversations").value;failedRequest=null;$("retry").hidden=true;await loadChat();});
$("rename-chat").onclick=()=>{if(busy||!conversation)return;$("conversation-title-form").hidden=false;$("conversation-title").focus();$("conversation-title").select();};
$("cancel-title").onclick=()=>{$("conversation-title-form").hidden=true;$("rename-chat").focus();};
$("conversation-title-form").onsubmit=event=>{event.preventDefault();const title=$("conversation-title").value.trim();if(!title)return;action(event.submitter,async()=>{await api(`/conversations/${conversation}/title`,"PATCH",{title,revision:titleRevision});await loadChat();await loadConversations();$("rename-chat").focus();});};
$("delete-chat").onclick=()=>{if(!confirm("删除这段聊天？已保存的运动记录、计划和偏好会保留。"))return;action($("delete-chat"),async()=>{await api(`/conversations/${conversation}`,"DELETE");await newConversation();});};
$("chat-form").onsubmit=event=>{event.preventDefault();const message=$("message").value.trim();if(!message)return;action(event.submitter,async()=>sendRequest({key:crypto.randomUUID(),message,conversation}));};
$("retry").onclick=()=>action($("retry"),async()=>{if(failedRequest)await sendRequest(failedRequest);});
$("week-form").onsubmit=event=>{event.preventDefault();action(event.submitter,loadPlan);};
$("records-form").onsubmit=event=>{event.preventDefault();action(event.submitter,loadRecords);};
$("activity-form").onsubmit=event=>{event.preventDefault();action(event.submitter,async()=>candidate(await api("/activities/actions","POST",{activity_id:$("activity-id").value,occurred_on:$("activity-day").value,minutes:Number($("activity-minutes").value)},crypto.randomUUID()),$("activity-candidate")));};
days.forEach((day,i)=>{const label=element("label",day),input=element("input");input.type="checkbox";input.name="weekday";input.value=i;label.prepend(input);$("weekdays").append(label);});
$("profile-form").onsubmit=event=>{event.preventDefault();action(event.submitter,async()=>{const patch={};if($("goal").value.trim())patch.goal=$("goal").value.trim();if($("preference").value)patch.preferred_activities=[$("preference").value];if($("frequency").value)patch.weekly_sessions=Number($("frequency").value);if($("duration").value)patch.session_minutes=Number($("duration").value);const selected=[...document.querySelectorAll("[name=weekday]:checked")].map(n=>Number(n.value));if(selected.length)patch.available_weekdays=selected;if(!Object.keys(patch).length)throw new Error("请填写至少一项；删除已保存信息请点“忘记这一项”。");candidate(await api("/profile/actions","POST",patch,crypto.randomUUID()),$("profile-candidate"));});};

