"use strict";
let mediaJob=null,mediaTimer=null,mediaPreviewUrl=null,mediaGeneration=0,mediaSelected=null;
const mediaTasks={nutrition_label:"营养标签",meal_photo:"餐食照片",workout_video:"运动视频"};
const mediaStatuses={queued:"等待处理",running:"正在分析",succeeded:"已分析，等待核对",failed:"分析未完成",cancelled:"已取消"};
function clearMedia(){mediaGeneration++;mediaSelected=null;clearTimeout(mediaTimer);mediaJob=null;if(mediaPreviewUrl)URL.revokeObjectURL(mediaPreviewUrl);mediaPreviewUrl=null;["media-jobs","media-result","media-preview","media-candidate","domain-output"].forEach(id=>$(id).replaceChildren());$("media-file").value="";$("media-message").value="";}
async function loadMediaJobs(){if(!token)return;const generation=mediaGeneration;const data=await api("/media/jobs");if(!token||generation!==mediaGeneration)return;const parent=$("media-jobs");parent.replaceChildren();data.jobs.forEach(job=>{const b=element("button",`${mediaTasks[job.task]} · ${mediaStatuses[job.status]} · ${job.progress}%`,"secondary");b.onclick=()=>action(b,()=>showMediaJob(job.job_id));parent.append(b);});}
async function previewMedia(job){
  const generation=mediaGeneration;
  if(mediaPreviewUrl)URL.revokeObjectURL(mediaPreviewUrl);mediaPreviewUrl=null;$("media-preview").replaceChildren();
  const response=await fetch(`/media/${job.asset_id}/content`,{headers:{Authorization:`Bearer ${token}`},cache:"no-store"});
  if(!response.ok)return;const blob=await response.blob();if(!token||generation!==mediaGeneration||mediaSelected!==job.job_id)return;mediaPreviewUrl=URL.createObjectURL(blob);
  const node=element(job.task==="workout_video"?"video":"img");node.src=mediaPreviewUrl;node.className="media-preview";if(node.tagName==="VIDEO")node.controls=true;else node.alt="本次上传素材";$("media-preview").append(node);
}
function inputField(parent,label,value,type="text"){
  const wrap=element("label",label),input=element("input");input.type=type;input.value=value??"";if(type==="number"){input.min="0";input.step="any";}wrap.append(input);parent.append(wrap);return input;
}
function numberValue(input){return input.value===""?null:Number(input.value);}
async function showMediaJob(id){
  clearTimeout(mediaTimer);mediaSelected=id;const generation=mediaGeneration;const job=await api(`/media/jobs/${id}`);if(!token||generation!==mediaGeneration||mediaSelected!==id)return;mediaJob=job;
  const parent=$("media-result");parent.replaceChildren(element("h2",`${mediaTasks[job.task]} · ${mediaStatuses[job.status]}`),element("p",`进度 ${job.progress}%；${job.note}`));
  if(job.status==="queued"||job.status==="running"){
    const cancel=element("button","取消后续处理","secondary");parent.append(cancel);cancel.onclick=()=>action(cancel,async()=>{await api(`/media/jobs/${id}/cancel`,"POST");await showMediaJob(id);});
    mediaTimer=setTimeout(()=>{if(token)showMediaJob(id).catch(error=>notice(error.message));},1500);return;
  }
  if(job.status==="failed"){
    parent.append(element("p","已保留任务，可在剩余调用额度内重试。没有保存业务记录。"));
    const retry=element("button","重试分析","secondary");parent.append(retry);retry.onclick=()=>action(retry,async()=>{await api(`/media/jobs/${id}/retry`,"POST");await showMediaJob(id);});return;
  }
  if(job.status!=="succeeded")return;
  await previewMedia(job);if(!token||generation!==mediaGeneration||mediaSelected!==id)return;parent.append(element("p",job.result.uncertainty||"请对照原素材核对，识别结果仍可能有误。"));
  const form=element("form",undefined,"grid");parent.append(form);let readResult,recordData;
  if(job.task==="nutrition_label"){
    const r=job.result,name=inputField(form,"食物名称",r.name),basis=inputField(form,"标签计量基准",r.basis_amount,"number");
    const unit=inputField(form,"计量单位（g 或 ml）",r.unit),energy=inputField(form,"标签热量",r.energy_value,"number"),energyUnit=inputField(form,"热量单位（kJ 或 kcal）",r.energy_unit);
    const protein=inputField(form,"标签蛋白质 g",r.protein_g,"number"),carbs=inputField(form,"标签碳水 g",r.carbs_g,"number"),fat=inputField(form,"标签脂肪 g",r.fat_g,"number");
    readResult=()=>({...r,name:name.value||null,basis_amount:numberValue(basis),unit:unit.value||null,energy_value:numberValue(energy),energy_unit:energyUnit.value||null,protein_g:numberValue(protein),carbs_g:numberValue(carbs),fat_g:numberValue(fat)});
    recordData=()=>{const v=readResult();if([v.name,v.unit,v.energy_unit,v.basis_amount,v.energy_value,v.protein_g,v.carbs_g,v.fat_g].some(x=>x===null)||v.basis_amount<=0)throw new Error("请先补齐名称、计量基准、单位和四项营养值。");return {name:v.name,unit:v.unit,energy_kcal:v.energy_value*(v.energy_unit==="kJ"?1/4.184:1)*100/v.basis_amount,protein_g:v.protein_g*100/v.basis_amount,carbs_g:v.carbs_g*100/v.basis_amount,fat_g:v.fat_g*100/v.basis_amount,source:"上传营养标签，经本人核对"};};
  }else if(job.task==="meal_photo"){
    const r=job.result,rows=[];const day=inputField(form,"餐食日期",today,"date"),mealType=inputField(form,"餐别（早餐／午餐／晚餐／加餐）","");
    const list=element("div",undefined,"wide");form.append(list);
    function add(item={name:"",amount_g:null,estimated_energy_kcal:null}){const row=element("div",undefined,"grid");list.append(row);rows.push({name:inputField(row,"食物",item.name),amount:inputField(row,"估计重量 g（未知可留空）",item.amount_g,"number"),energy:inputField(row,"估计热量 kcal（未知可留空）",item.estimated_energy_kcal,"number")});}
    r.items.forEach(add);if(!rows.length)add();const addButton=element("button","增加一种食物","secondary");addButton.type="button";form.append(addButton);addButton.onclick=()=>add();
    readResult=()=>({...r,items:rows.filter(x=>x.name.value.trim()).map(x=>({name:x.name.value.trim(),amount_g:numberValue(x.amount),estimated_energy_kcal:numberValue(x.energy)}))});
    recordData=()=>({record_date:day.value,meal_type:mealType.value.trim(),items:readResult().items,source:"vision_estimate_user_confirmed"});
  }else{
    const r=job.result;r.observations.forEach(o=>parent.append(element("p",`${o.start_sec}–${o.end_sec} 秒：${o.name}；${o.observation}。稀疏采样不提供整段准确次数。`)));
    const name=inputField(form,"要记录的训练名称",r.observations[0]?.name||""),day=inputField(form,"训练日期",today,"date"),duration=inputField(form,"本人确认的实际分钟（未知留空）",null,"number"),feedback=inputField(form,"本人训练反馈","");
    readResult=()=>r;recordData=()=>({record_date:day.value,items:[{category:"其他",name:name.value,duration_min:numberValue(duration)}],feedback:feedback.value||null});
  }
  const review=element("button",job.reviewed?"更新核对结果":"我已核对，保存核对结果");review.type="submit";form.append(review);
  form.onsubmit=event=>{event.preventDefault();action(review,async()=>{await api(`/media/jobs/${id}/review`,"PATCH",{version:job.version,result:readResult()});await showMediaJob(id);await loadMediaJobs();});};
  if(job.reviewed){
    const propose=element("button","生成待确认记录","secondary");propose.type="button";form.append(propose);
    propose.onclick=()=>action(propose,async()=>{candidate(await api(`/media/jobs/${id}/actions`,"POST",{version:job.version,data:recordData()},crypto.randomUUID()),$("media-candidate"));});
  }
  const remove=element("button","删除上传源文件","quiet");parent.append(remove);remove.onclick=()=>action(remove,async()=>{await api(`/media/${job.asset_id}`,"DELETE");$("media-preview").replaceChildren();if(mediaPreviewUrl)URL.revokeObjectURL(mediaPreviewUrl);mediaPreviewUrl=null;notice("源文件已删除；已确认记录保留。");});
}
$("media-form").onsubmit=event=>{event.preventDefault();action(event.submitter,async()=>{
  const file=$("media-file").files[0];if(!file)throw new Error("请选择素材。");const task=$("media-task").value,kind=task==="workout_video"?"video":"image";
  if(file.size>(kind==="image"?8:80)*1024*1024)throw new Error("图片最多8MB，视频最多80MB。");
  const response=await fetch(`/media/uploads?kind=${kind}`,{method:"POST",headers:{Authorization:`Bearer ${token}`,"Content-Type":"application/octet-stream"},body:file});
  if(!response.ok)throw new Error("上传未完成，请检查格式、大小和视频时长。");const asset=await response.json();
  const job=await api(`/media/${asset.asset_id}/jobs`,"POST",{task,message:$("media-message").value},crypto.randomUUID());
  $("media-candidate").replaceChildren();await showMediaJob(job.job_id);await loadMediaJobs();
});};
$("media-refresh").onclick=()=>action($("media-refresh"),loadMediaJobs);
$("domain-form").onsubmit=event=>{event.preventDefault();action(event.submitter,async()=>{const data=await api(`/domain-records/${$("domain-type").value}?start_date=${$("domain-start").value}&end_date=${$("domain-end").value}`);const parent=$("domain-output");parent.replaceChildren(element("p",data.note||"查询结果"));data.records.forEach(r=>{const row=element("div",undefined,"candidate");renderOperation("domain_record",{record_type:r.record_type,values:r.values},row);const remove=element("button","删除这条记录","quiet");row.append(remove);remove.onclick=()=>action(remove,async()=>candidate(await api(`/domain-records/${r.id}/deletion-actions`,"POST",{},crypto.randomUUID()),row));parent.append(row);});if(!data.records.length)parent.append(element("p","此范围暂无记录，不代表实际没有活动或摄入。"));});};
$("domain-start").value=$("domain-end").value=today;
