const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const isoDate=d=>[d.getFullYear(),String(d.getMonth()+1).padStart(2,'0'),String(d.getDate()).padStart(2,'0')].join('-');
let today=isoDate(new Date());let cache={},calendarMonth=new Date(),mealFilter='all',refreshSequence=0;
const titles={overview:'Good day.',calendar:'Planner',fitness:'Fitness',bills:'Bills & payments',care:'Routines',meals:'Meals',settings:'Settings & Telegram'};
let selectedCalendarDate=today;
function formatDate(v){if(!v)return '—';const m=String(v).match(/^(\d{4})-(\d{2})-(\d{2})/);return m?`${m[3]}/${m[2]}/${m[1].slice(-2)}`:String(v)}
function formatDateTime(v){if(!v)return '—';const s=String(v);return `${formatDate(s)}${s.includes('T')?' '+s.slice(11,16):''}`}
function parseDateInput(v){const s=String(v||'').trim();if(!s)return '';let m=s.match(/^(\d{4})-(\d{2})-(\d{2})$/);if(m)return s;m=s.match(/^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2}|\d{4})$/);if(!m)throw Error('Enter the date as DD/MM/YY, for example 16/11/26.');let [,dd,mm,yy]=m;let year=Number(yy);if(yy.length===2)year+=2000;const d=new Date(year,Number(mm)-1,Number(dd));if(d.getFullYear()!==year||d.getMonth()!==Number(mm)-1||d.getDate()!==Number(dd))throw Error('Please enter a valid date.');return `${year}-${String(mm).padStart(2,'0')}-${String(dd).padStart(2,'0')}`}
function displayDateInput(v=today){return formatDate(v)}
function parseDateTimeInput(v){const s=String(v||'').trim();if(!s)return '';if(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(s))return s;const m=s.match(/^(.+?)\s+(\d{1,2}):(\d{2})$/);if(!m)throw Error('Enter date and time as DD/MM/YY HH:MM.');const date=parseDateInput(m[1]),hour=Number(m[2]),minute=Number(m[3]);if(hour>23||minute>59)throw Error('Please enter a valid time.');return `${date}T${String(hour).padStart(2,'0')}:${String(minute).padStart(2,'0')}`}
function displayDateTimeInput(v){return v?formatDateTime(v):''}
$('#today-label').textContent=`${new Date().toLocaleDateString(undefined,{weekday:'long'}).toUpperCase()} · ${formatDate(today)}`;
function longDateLabel(value){const d=new Date(value+'T12:00:00');return `${d.toLocaleDateString(undefined,{weekday:'long'})} · ${formatDate(value)}`}
function syncCurrentDate(){const next=isoDate(new Date());if(next===today)return;const previous=today,wasViewingToday=selectedCalendarDate===previous;today=next;if(wasViewingToday)selectedCalendarDate=next;const currentMonth=new Date();if(calendarMonth.getFullYear()===new Date(previous+'T12:00:00').getFullYear()&&calendarMonth.getMonth()===new Date(previous+'T12:00:00').getMonth())calendarMonth=new Date(currentMonth.getFullYear(),currentMonth.getMonth(),1);$('#today-label').textContent=`${currentMonth.toLocaleDateString(undefined,{weekday:'long'}).toUpperCase()} · ${formatDate(today)}`;$$('.date-entry').forEach(input=>{if(input.value===displayDateInput(previous))input.value=displayDateInput(today)})}
async function api(path,opts={}){let r=await fetch('/api/'+path,{headers:{'Content-Type':'application/json'},...opts});let d=await r.json().catch(()=>({}));if(!r.ok)throw Error(d.detail||'Request failed');return d}
function toast(s){let t=$('#toast');t.textContent=s;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2400)}
function money(n){return Number(n||0).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})+' MAD'}
function billCountdown(bill){if(!bill)return '—';let days=Number(bill.days_left);if(!Number.isFinite(days)&&bill.due_date){const due=new Date(String(bill.due_date).slice(0,10)+'T12:00:00'),base=new Date(today+'T12:00:00');if(Number.isFinite(due.getTime()))days=Math.round((due-base)/86400000)}if(!Number.isFinite(days))return 'Date unavailable';if(days<0)return `Overdue ${Math.abs(days)} ${Math.abs(days)===1?'day':'days'}`;if(days===0)return 'Due today';return `${days} ${days===1?'day':'days'} left`;}
function formatKg(n){return Number(n||0).toLocaleString('fr-MA',{minimumFractionDigits:2,maximumFractionDigits:2})}
function empty(s='Nothing here yet. Add your first record to get started.'){return `<div class="empty">${s}</div>`}
function item(title,sub='',tag='',actions=''){return `<div class="row-item"><div class="row-main"><b>${esc(title)}</b><small>${esc(sub)}</small></div>${tag?`<span class="tag">${esc(tag)}</span>`:''}${actions?`<div class="action-group">${actions}</div>`:''}</div>`}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function stat(label,value,hint=''){return `<div class="stat"><div class="label">${label}</div><div class="value">${value}</div><div class="hint">${hint}</div></div>`}
function uniqueWeightsPerDay(weights){const byDay=new Map();for(const w of [...weights].sort((a,b)=>Number(a.id)-Number(b.id)))byDay.set(w.date,w);return [...byDay.values()].sort((a,b)=>a.date.localeCompare(b.date)||Number(a.id)-Number(b.id))}
function weightLatest(weights){return uniqueWeightsPerDay(weights).slice(-1)[0]}
function estimateCalories(duration,weights){const latest=weightLatest(weights);const kg=latest?Number(latest.kg):70;return Math.round((3.5*3.5*kg/200)*Number(duration||0))}
function updateTreadmillPreview(){const d=Number($('#treadmill-form [name="duration_min"]')?.value||0);const el=$('#treadmill-calorie-preview');if(!el)return;const kg=weightLatest(cache.weights||[]);if(d>0){el.textContent=`${estimateCalories(d,cache.weights||[])} kcal`;el.title=kg?`Estimate based on ${formatKg(kg.kg)} kg`: 'Estimate uses a 70 kg default because no weight is saved yet.'}else el.textContent='— kcal'}
function weekStartISO(v){const d=new Date(v+'T12:00:00');d.setDate(d.getDate()-((d.getDay()+6)%7));return isoDate(d)}
function addDaysISO(v,days){const d=new Date(v+'T12:00:00');d.setDate(d.getDate()+days);return isoDate(d)}
const WEEKDAY_LABELS=['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'];
function movableHomeReservation(recipe,day,meals=cache.daily_meals||[]){
 if(recipe.meal_source==='outside')return null;
 const ready=Math.max(0,Number(recipe.plates_ready||0));
 const reserved=meals.filter(m=>m.status==='pending'&&Number(m.recipe_id)===Number(recipe.id)&&m.plan_date>=today&&m.plan_date!==day);
 if(ready>reserved.length)return null;
 return reserved.filter(m=>m.plan_date>day&&['automatic','unknown'].includes(m.assignment_source||'unknown')).sort((a,b)=>b.plan_date.localeCompare(a.plan_date))[0]||null;
}
function recipeCanBeAssignedForDay(recipe,day,meals=cache.daily_meals||[]){
 const weekdayIndex=(new Date(day+'T12:00:00').getDay()+6)%7;
 if((recipe.excluded_weekdays||[]).map(Number).includes(weekdayIndex))return false;
 if(recipe.meal_source==='outside')return true;
 const ready=Math.max(0,Number(recipe.plates_ready||0));
 if(ready<=0)return false;
 const reserved=meals.filter(m=>m.status==='pending'&&Number(m.recipe_id)===Number(recipe.id)&&m.plan_date>=today&&m.plan_date!==day);
 if(ready>reserved.length)return true;
 return meals.some(m=>m.status==='pending'&&Number(m.recipe_id)===Number(recipe.id)&&m.plan_date>day&&['automatic','unknown'].includes(m.assignment_source||'unknown'));
}
function renderFitnessStats(treadmills,workouts,weights){
 const all=[...treadmills.map(x=>({date:x.date})),...workouts.map(x=>({date:x.date}))];
 const thisMonth=all.filter(x=>(x.date||'').slice(0,7)===today.slice(0,7)).length;
 const activeWeeks=new Set(all.map(x=>weekStartISO(x.date)));
 let streak=0,week=new Date(weekStartISO(today)+'T12:00:00');while(activeWeeks.has(isoDate(week))&&streak<520){streak++;week.setDate(week.getDate()-7)}
 const thisWeek=weekStartISO(today);
 const thisWeekWeights=uniqueWeightsPerDay(weights).filter(x=>x.date>=thisWeek&&x.date<=today);
 const weeklyAverage=thisWeekWeights.length?thisWeekWeights.reduce((sum,x)=>sum+Number(x.kg),0)/thisWeekWeights.length:null;
 const start=new Date();start.setDate(start.getDate()-83);const startISO=isoDate(start);
 const treadmillMins=treadmills.filter(x=>x.date>=startISO&&x.date<=today).reduce((a,x)=>a+Number(x.duration_min||0),0);
 const gymCount=workouts.filter(x=>x.date>=startISO&&x.date<=today).length;
 $('#fitness-stats').innerHTML=stat('Workouts',all.length,'Treadmill + gym')+stat('This month',thisMonth,'Sessions')+stat('Week streak',streak,'Consecutive active weeks')+stat('This week avg weight',weeklyAverage===null?'—':`${formatKg(weeklyAverage)} kg`,`${thisWeekWeights.length} weight ${thisWeekWeights.length===1?'entry':'entries'} this week`)+stat('Treadmill avg / week',`${Math.round(treadmillMins/12)} min`,'Last 12 weeks')+stat('Gym avg / week',`${(gymCount/12).toFixed(1)}`,'Sessions · last 12 weeks');
}
function renderWeightHistory(weights){
 const sorted=[...uniqueWeightsPerDay(weights)].sort((a,b)=>b.date.localeCompare(a.date)||Number(b.id)-Number(a.id));
 $('#weight-history-count').textContent=`${sorted.length} entries`;
 $('#weight-history').innerHTML=sorted.map(w=>`<div class="history-entry"><div class="history-entry-main"><b>${formatDate(w.date)}</b><small>Recorded weight</small></div><div class="history-entry-side"><strong>${formatKg(w.kg)} kg</strong></div></div>`).join('')||empty('No weight records yet.');
}
function renderWeightGraph(weights){
 const host=$('#weight-graph');if(!host)return;
 const sorted=uniqueWeightsPerDay(weights);
 if(!sorted.length){host.innerHTML=empty('Record your weight to see your progress graph.');return}
 const W=960,H=280,L=62,R=28,T=22,B=48,plotW=W-L-R,plotH=H-T-B;
 const vals=sorted.map(x=>Number(x.kg)).filter(Number.isFinite);let min=Math.min(...vals),max=Math.max(...vals);
 if(min===max){min-=0.5;max+=0.5}else{const pad=Math.max((max-min)*0.12,0.25);min-=pad;max+=pad}
 const xAt=i=>sorted.length===1?L+plotW/2:L+(i/(sorted.length-1))*plotW;
 const yAt=v=>T+((max-v)/(max-min))*plotH;
 const grid=Array.from({length:5},(_,i)=>{const value=min+(max-min)*i/4,y=yAt(value);return `<line class="weight-grid-line" x1="${L}" y1="${y}" x2="${W-R}" y2="${y}"/><text class="weight-axis-label" x="${L-10}" y="${y+4}" text-anchor="end">${value.toFixed(1)}</text>`}).join('');
 const points=sorted.map((r,i)=>`${xAt(i)},${yAt(Number(r.kg))}`).join(' ');
 const dots=sorted.map((r,i)=>`<circle class="weight-point" cx="${xAt(i)}" cy="${yAt(Number(r.kg))}" r="5.5" tabindex="0" role="img" data-date="${esc(formatDate(r.date))}" data-weight="${esc(formatKg(r.kg))}"><title>${formatDate(r.date)} · ${formatKg(r.kg)} kg</title></circle>`).join('');
 const tickCount=Math.min(6,sorted.length),ticks=Array.from({length:tickCount},(_,i)=>{const ix=tickCount===1?0:Math.round(i*(sorted.length-1)/(tickCount-1));return `<text class="weight-axis-label" x="${xAt(ix)}" y="${H-15}" text-anchor="middle">${formatDate(sorted[ix].date)}</text>`}).join('');
 const latest=sorted[sorted.length-1];
 host.innerHTML=`<div class="weight-chart-scroll"><div class="weight-chart-wrap"><svg class="weight-chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Weight progress graph, ${sorted.length} records, latest ${formatKg(latest.kg)} kilograms"><defs><linearGradient id="weight-area" x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stop-color="var(--green)" stop-opacity=".24"/><stop offset="100%" stop-color="var(--green)" stop-opacity="0"/></linearGradient></defs>${grid}<polygon class="weight-area" points="${L},${T+plotH} ${points} ${W-R},${T+plotH}"/><polyline class="weight-line" points="${points}"/>${dots}${ticks}</svg><div class="weight-chart-tooltip" role="status" hidden></div></div></div><div class="weight-chart-meta"><span>First: ${formatKg(sorted[0].kg)} kg · ${formatDate(sorted[0].date)}</span><b>Latest: ${formatKg(latest.kg)} kg · ${formatDate(latest.date)}</b></div>`;
 const wrap=host.querySelector('.weight-chart-wrap'),tip=host.querySelector('.weight-chart-tooltip');
 const showTip=(point,event)=>{const rect=wrap.getBoundingClientRect();const px=event&&Number.isFinite(event.clientX)?event.clientX:point.getBoundingClientRect().left+point.getBoundingClientRect().width/2;const py=event&&Number.isFinite(event.clientY)?event.clientY:point.getBoundingClientRect().top;tip.innerHTML=`<b>${point.dataset.weight} kg</b><span>${point.dataset.date}</span>`;tip.hidden=false;tip.style.left=`${Math.max(8,Math.min(rect.width-145,px-rect.left+12))}px`;tip.style.top=`${Math.max(4,Math.min(rect.height-58,py-rect.top-52))}px`};
 const hideTip=()=>{tip.hidden=true};
 wrap.querySelectorAll('.weight-point').forEach(point=>{point.addEventListener('pointerenter',e=>showTip(point,e));point.addEventListener('pointermove',e=>showTip(point,e));point.addEventListener('pointerleave',hideTip);point.addEventListener('focus',()=>showTip(point));point.addEventListener('blur',hideTip);point.addEventListener('click',e=>showTip(point,e))});
}
function renderWeeklyWeightAverages(weights){
 const host=$('#weekly-weight-average');if(!host)return;
 const grouped=new Map();
 for(const w of uniqueWeightsPerDay(weights)){const start=weekStartISO(w.date);if(!grouped.has(start))grouped.set(start,[]);grouped.get(start).push(Number(w.kg))}
 const weeks=[...grouped.entries()].sort((a,b)=>a[0].localeCompare(b[0])).map(([start,values])=>({start,avg:values.reduce((a,b)=>a+b,0)/values.length,count:values.length}));
 if(!weeks.length){host.innerHTML=empty('Add weight records to compare weekly averages.');return}
 const min=Math.min(...weeks.map(x=>x.avg)),max=Math.max(...weeks.map(x=>x.avg)),span=max-min||1;
 const bars=weeks.map((w,i)=>{const height=38+((w.avg-min)/span)*132;const end=new Date(w.start+'T12:00:00');end.setDate(end.getDate()+6);const endISO=isoDate(end);const tip=`Week ${formatDate(w.start)}–${formatDate(endISO)} · average ${formatKg(w.avg)} kg · ${w.count} ${w.count===1?'entry':'entries'}`;return `<div class="week-average-column" title="${esc(tip)}" aria-label="${esc(tip)}"><span class="week-average-value">${formatKg(w.avg)}</span><div class="week-average-track"><i style="height:${height}px"></i></div><small>${i%4===0||i===weeks.length-1?formatDate(w.start):''}</small></div>`}).join('');
 const minChartWidth=Math.max(320,weeks.length*52);
 host.innerHTML=`<div class="weekly-average-summary"><span>${weeks.length} weeks with recorded weight</span><b>Latest week: ${formatKg(weeks[weeks.length-1].avg)} kg</b></div><div class="weekly-average-scroll"><div class="weekly-average-chart" style="--week-count:${weeks.length};min-width:${minChartWidth}px">${bars}</div></div><p class="muted weekly-average-help">Each bar is the average of all weigh-ins recorded during that Monday–Sunday week. The chart fills the card; older weeks remain available by horizontal scrolling.</p>`;
}
function renderHeatmap(id,records,kind){
 const host=$(id);if(!host)return;const values={};for(const r of records){const key=r.date;if(!key)continue;values[key]=(values[key]||0)+(kind==='minutes'?Number(r.duration_min||0):1)}
 const end=new Date(today+'T12:00:00'),start=new Date(end);start.setDate(start.getDate()-364);start.setDate(start.getDate()-start.getDay());
 const days=[];for(let d=new Date(start);d<=end;d.setDate(d.getDate()+1)){const date=isoDate(d),value=values[date]||0;let level=0;if(value>0){if(kind==='minutes')level=value<30?1:value<60?2:value<90?3:4;else level=value===1?1:value===2?2:value===3?3:4}days.push({date,value,level,week:Math.floor((d-start)/86400000/7),weekday:d.getDay(),month:d.getMonth()})}
 const weeks=Math.max(...days.map(x=>x.week))+1;let monthHtml='',seen=-1;for(const day of days){if(day.month!==seen&&day.weekday===0){const label=new Date(day.date+'T12:00:00').toLocaleDateString(undefined,{month:'short'});monthHtml+=`<span style="grid-column:${day.week+1}/span 3">${label}</span>`;seen=day.month}}
 const cells=days.map(x=>`<span class="heat-cell level-${x.level}" style="grid-column:${x.week+1};grid-row:${x.weekday+1}" title="${formatDate(x.date)} · ${kind==='minutes'?`${x.value} min`:`${x.value} gym session${x.value===1?'':'s'}`}" aria-label="${formatDate(x.date)} ${x.value}"></span>`).join('');
 const rangeStart=isoDate(new Date(end.getTime()-364*86400000));
 const period=records.filter(x=>x.date>=rangeStart&&x.date<=today);
 const total=kind==='minutes'?period.reduce((a,x)=>a+Number(x.duration_min||0),0):period.length;
 host.innerHTML=`<div class="heatmap-scroll"><div class="heatmap-months" style="--weeks:${weeks}">${monthHtml}</div><div class="heatmap-grid" style="--weeks:${weeks}">${cells}</div></div><div class="heatmap-footer"><span>${kind==='minutes'?`${Math.round(total)} minutes trained`:`${total} gym sessions`} · ${period.length} entries in view</span><span class="heat-legend">Less <i class="level-0"></i><i class="level-1"></i><i class="level-2"></i><i class="level-3"></i><i class="level-4"></i> More</span></div>`;
}
async function refresh(){const requestId=++refreshSequence;try{
 syncCurrentDate();
 const mealWeekStart=weekStartISO(today),mealWeekEnd=addDaysISO(mealWeekStart,6);
 const [sum,events,tasks,weights,treadmills,workouts,workout_plan,bills,routines,recipes,dailyMeals,shoppingItems,cookReminders,mealSkips]=await Promise.all([
  'summary','events?limit=10000','tasks?limit=10000','weights?limit=10000','treadmill?limit=10000','workouts?limit=10000','workout_plan','bills?limit=10000','routines?limit=10000','recipes?limit=10000','daily_meals?limit=10000','shopping_items?limit=10000','meal_cook_reminders?limit=10000',`meal-schedule-skips?start_date=${mealWeekStart}&end_date=${mealWeekEnd}`
 ].map(x=>api(x)));
 if(requestId!==refreshSequence)return;
 cache={events,tasks,weights,treadmill:treadmills,workouts,workout_plan,bills,routines,recipes,daily_meals:dailyMeals,shopping_items:shoppingItems,meal_cook_reminders:cookReminders,meal_schedule_skips:mealSkips};$('#api-status').textContent='API connected';
 const nextBill=sum.upcoming_bill;
 const billHint=nextBill?`${esc(nextBill.name)} · ${formatDate(nextBill.due_date)} · ${money(nextBill.amount_mad)}`:'No unpaid bills coming up';
 const routinesDueToday=routines.filter(r=>r.next_due&&r.next_due<=today).length;
 const tomorrowDate=new Date();tomorrowDate.setDate(tomorrowDate.getDate()+1);
 const tomorrowISO=isoDate(tomorrowDate);
 const tomorrowSchedules = events.filter(e=>String(e.start_at||'').slice(0,10)===tomorrowISO).length
  + tasks.filter(t=>t.status!=='done'&&t.due_date===tomorrowISO).length
  + routines.filter(r=>r.next_due===tomorrowISO).length
  + dailyMeals.filter(m=>m.plan_date===tomorrowISO).length
  + cookReminders.filter(r=>r.due_date===tomorrowISO&&r.status==='open').length
  + bills.filter(b=>b.status!=='paid'&&b.due_date===tomorrowISO).length;
 $('#stats').innerHTML=stat('Routines',routinesDueToday,'Due or overdue')+stat('Tomorrow schedules',tomorrowSchedules,'All scheduled items')+stat('Latest weight',sum.latest_weight?formatKg(sum.latest_weight.kg)+' kg':'—','Fitness log')+stat('Upcoming bills',billCountdown(nextBill),billHint);
 renderTodayMeal();
 let attention=[];tasks.filter(t=>t.status!=='done').sort((a,b)=>(a.due_date||'9999').localeCompare(b.due_date||'9999')||(a.due_time||'99:99').localeCompare(b.due_time||'99:99')).slice(0,4).forEach(t=>attention.push(item(t.title,(t.due_date?formatDate(t.due_date):'No due date')+(t.due_time?' · '+t.due_time:''),t.priority)));bills.filter(b=>b.status!=='paid').sort((a,b)=>a.due_date.localeCompare(b.due_date)).slice(0,3).forEach(b=>attention.push(item(b.name+' · '+money(b.amount_mad),'Due '+formatDate(b.due_date),'Unpaid')));$('#attention').innerHTML=attention.join('')||empty('Nothing urgent.');
 $('#fitness-snapshot').innerHTML=`<div><div class="big-number">${sum.latest_weight?formatKg(sum.latest_weight.kg)+' kg':'—'}</div><small class="muted">Latest weight</small></div><div><div class="big-number">${treadmills.length}</div><small class="muted">Treadmill sessions</small></div><div><div class="big-number">${workouts.length}</div><small class="muted">Gym workouts</small></div>`;
 $('#routine-snapshot').innerHTML=[...routines].sort((a,b)=>(a.next_due||'9999').localeCompare(b.next_due||'9999')).slice(0,4).map(r=>item(r.name,`Next: ${r.next_due?formatDate(r.next_due):'Not scheduled'} · ${r.routine_time||'—'} · Late ${r.routine_late_time||'off'}${r.routine_late_time&&Number(r.routine_rollover_after_late)?' · auto-moves tomorrow':''}`,r.interval_days+' day interval',`<button class="mini-btn" data-complete="${r.id}">Done</button>`)).join('')||empty();
 renderCalendar(events);
 renderWeightHistory(weights);renderWeightGraph(weights);renderWeeklyWeightAverages(weights);
 $('#treadmill-history-count').textContent=`${treadmills.length} sessions`;
 $('#treadmill-history').innerHTML=[...treadmills].sort((a,b)=>b.date.localeCompare(a.date)||Number(b.id)-Number(a.id)).map(t=>`<div class="history-entry"><div class="history-entry-main"><b>${Number(t.duration_min)} min · ${Number(t.distance_km).toFixed(2)} km</b><small>${formatDate(t.date)} · ${Number(t.speed_kmh||5).toFixed(0)} km/h · 0% incline</small></div><div class="history-entry-side"><strong>${Number(t.calories_kcal||0)} kcal</strong><small>Estimated</small></div></div>`).join('')||empty();
 $('#workout-history-count').textContent=`${workouts.length} workouts`;
 $('#workout-history').innerHTML=[...workouts].sort((a,b)=>b.date.localeCompare(a.date)||Number(b.id)-Number(a.id)).map(w=>`<div class="history-entry"><div class="history-entry-main"><b>${esc(w.name)}</b><small>${formatDate(w.date)}${w.notes?' · '+esc(w.notes):''}</small></div><div class="history-entry-side"><span class="tag">Gym</span><button class="mini-btn" data-delete="workouts:${w.id}">Delete</button></div></div>`).join('')||empty();
 renderFitnessStats(treadmills,workouts,weights);renderHeatmap('#treadmill-activity',treadmills,'minutes');renderHeatmap('#gym-activity',workouts,'count');updateTreadmillPreview();
 $('#workout-plan-list').innerHTML=workout_plan.sort((a,b)=>['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'].indexOf(a.weekday)-['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'].indexOf(b.weekday)).map(w=>item(w.weekday,w.name,'Planned',`<button class="mini-btn" data-delete="workout_plan:${w.id}">Remove</button>`)).join('')||empty('No workout days planned yet.');
 $('#bill-summary').innerHTML=stat('Unpaid bills',sum.unpaid_bills,'Outstanding')+stat('Total outstanding',money(sum.unpaid_total_mad),'MAD')+stat('Open tasks',sum.open_tasks,'In Planner');
 $('#bills-list').innerHTML=[...bills].sort((a,b)=>(a.status==='paid')-(b.status==='paid')||a.due_date.localeCompare(b.due_date)).map(b=>item(b.name,money(b.amount_mad)+' · Due '+formatDate(b.due_date),b.status,b.status!=='paid'?`<button class="mini-btn" data-pay="${b.id}">Mark paid</button>`:'' )).join('')||empty();
 $('#routines-list').innerHTML=routines.map(r=>{const rollover=r.routine_late_time&&Number(r.routine_rollover_after_late)===1;const lateLabel=r.routine_late_time?`Late alert ${r.routine_late_time}${rollover?' · auto-moves to tomorrow if still open':''}`:'Late alert off';return item(r.name,`Last: ${r.last_done?formatDate(r.last_done):'Not recorded'} · Next: ${r.next_due?formatDate(r.next_due):'—'} · Time ${r.routine_time||'—'} · ${lateLabel}`,r.interval_days+' days',`<button class="mini-btn" data-edit-routine="${r.id}">Edit routine</button><button class="mini-btn" data-edit-routine-record="${r.id}">Edit last record</button><button class="mini-btn" data-complete="${r.id}">Done</button>`)}).join('')||empty();
 renderMeals();renderPlannerShopping();
 }catch(e){if(requestId!==refreshSequence)return;$('#api-status').textContent='API unavailable';console.error(e);toast(e.message)}}
function calendarItemsForDay(day){
 const items=[];const c=cache;
 for(const e of (c.events||[]).filter(x=>x.start_at.slice(0,10)===day))items.push({type:'event',sortTime:e.start_at.includes('T')?e.start_at.slice(11,16):null,text:`${e.start_at.includes('T')?e.start_at.slice(11,16)+' ':''}${e.title}`,detail:`Event${e.category?' · '+e.category:''}`});
 for(const t of (c.tasks||[]).filter(x=>x.status!=='done'&&x.due_date===day))items.push({type:'task',sortTime:t.due_time||null,text:`${t.due_time?t.due_time+' ':''}${t.title}`,detail:'Task reminder'});
 const weight=uniqueWeightsPerDay(c.weights||[]).find(x=>x.date===day);if(weight)items.push({type:'weight',sortTime:null,text:`${formatKg(weight.kg)} kg`,detail:'Recorded weight'});
 const tread=(c.treadmill||[]).filter(x=>x.date===day);if(tread.length){const mins=tread.reduce((a,x)=>a+Number(x.duration_min||0),0);items.push({type:'treadmill',sortTime:null,text:`Treadmill ${Math.round(mins)} min`,detail:`${tread.length} session${tread.length===1?'':'s'} · ${tread.reduce((a,x)=>a+Number(x.calories_kcal||0),0)} estimated kcal`})}
 for(const w of (c.workouts||[]).filter(x=>x.date===day))items.push({type:'gym',sortTime:null,text:`Gym: ${w.name}${Number(w.duration_min||0)>0?' · '+Number(w.duration_min)+' min':''}`,detail:w.notes||'Gym workout'});
 for(const r of (c.routines||[])){if(r.next_due===day)items.push({type:'care',sortTime:r.routine_time||null,text:`${r.routine_time?r.routine_time+' ':''}Routine: ${r.name}`,detail:`Scheduled routine · late reminder ${r.routine_late_time||'off'}${r.routine_late_time&&Number(r.routine_rollover_after_late)?' · moves to tomorrow if still incomplete':''} · every ${Math.max(1,Number(r.interval_days||1))} days`})}
 for(const b of (c.bills||[]).filter(x=>x.status!=='paid'&&x.due_date===day))items.push({type:'bill',sortTime:null,text:`Bill: ${b.name}`,detail:`${money(b.amount_mad)} due`});
 for(const m of (c.daily_meals||[]).filter(x=>x.plan_date===day))items.push({type:m.status==='eaten'?'meal-eaten':'meal',sortTime:null,text:`${m.status==='eaten'?'✓ ':'🍽 '}${m.recipe_name||'Meal'}`,detail:`Daily plate · ${m.status==='eaten'?'confirmed eaten':'waiting for confirmation'}${m.calories_kcal!=null?' · '+Number(m.calories_kcal)+' kcal':''}`});
 for(const r of (c.meal_cook_reminders||[]).filter(x=>x.due_date===day))items.push({type:'care',sortTime:null,text:`🍳 Cook: ${r.recipe_name}`,detail:'Home meal stock ran out · replenish ready plates'});
 // Items with explicit times come first, sorted from earliest to latest;
 // date-only logs follow them in a stable, predictable order.
 return items.map((item,index)=>({...item,_index:index})).sort((a,b)=>{
   const at=a.sortTime||'99:99',bt=b.sortTime||'99:99';
   return at.localeCompare(bt)||a._index-b._index;
 }).map(({_index,sortTime,...item})=>item);
}
function renderCalendar(events){
 const year=calendarMonth.getFullYear(),month=calendarMonth.getMonth();$('#calendar-month-label').textContent=calendarMonth.toLocaleDateString(undefined,{month:'long',year:'numeric'});
 const first=new Date(year,month,1),start=new Date(year,month,1-first.getDay());const heads=['Sun','Mon','Tue','Wed','Thu','Fri','Sat'].map(x=>`<div class="cal-head">${x}</div>`).join('');let cells='';
 for(let i=0;i<42;i++){
  const d=new Date(start);d.setDate(start.getDate()+i);const iso=isoDate(d);const entries=calendarItemsForDay(iso);const shown=entries.slice(0,4);
  cells+=`<div class="cal-day ${d.getMonth()!==month?'outside':''} ${iso===today?'today':''} ${iso===selectedCalendarDate?'selected':''} ${entries.length?'has-event':''}" data-calendar-date="${iso}" role="button" tabindex="0" title="${formatDate(iso)} · ${entries.length} item${entries.length===1?'':'s'}"><div class="day-number">${d.getDate()}</div><div class="cal-day-items">${shown.map(x=>`<span class="cal-event ${x.type}" title="${esc(x.detail)}">${esc(x.text)}</span>`).join('')}${entries.length>4?`<span class="cal-more">+${entries.length-4} more</span>`:''}</div></div>`;
 }
 $('#calendar-grid').innerHTML=heads+cells;
 const selectedItems=calendarItemsForDay(selectedCalendarDate);const selected=new Date(selectedCalendarDate+'T12:00:00');
 $('#calendar-selected-label').textContent=longDateLabel(selectedCalendarDate);
 $('#calendar-day-detail').innerHTML=selectedItems.length?selectedItems.map(x=>`<div class="calendar-detail-row"><span class="calendar-detail-dot ${x.type}"></span><div><b>${esc(x.text)}</b><small>${esc(x.detail)}</small></div></div>`).join(''):empty('Nothing scheduled or recorded on this day.');
}
function shiftCalendarMonth(delta){calendarMonth=new Date(calendarMonth.getFullYear(),calendarMonth.getMonth()+delta,1);selectedCalendarDate=isoDate(calendarMonth);renderCalendar(cache.events||[])}
$('#month-prev').onclick=()=>shiftCalendarMonth(-1);$('#month-next').onclick=()=>shiftCalendarMonth(1);$('#month-today').onclick=()=>{calendarMonth=new Date();selectedCalendarDate=today;renderCalendar(cache.events||[])};$('#calendar-day-today').onclick=()=>{calendarMonth=new Date();selectedCalendarDate=today;renderCalendar(cache.events||[])};$('#calendar-grid').addEventListener('keydown',e=>{if((e.key==='Enter'||e.key===' ')&&e.target.closest('[data-calendar-date]')){e.preventDefault();selectedCalendarDate=e.target.closest('[data-calendar-date]').dataset.calendarDate;renderCalendar(cache.events||[])}});
function navigate(page){$$('.page').forEach(p=>p.classList.toggle('active',p.id===page));$$('#nav button').forEach(b=>b.classList.toggle('active',b.dataset.page===page));document.body.classList.toggle('inner-page',page!=='overview');$('#page-title').textContent=titles[page]||'XROB'}
$('#nav').addEventListener('click',e=>{let b=e.target.closest('[data-page]');if(b)navigate(b.dataset.page)});
document.addEventListener('click',e=>{let calendarDay=e.target.closest('[data-calendar-date]');if(calendarDay){selectedCalendarDate=calendarDay.dataset.calendarDate;renderCalendar(cache.events||[])}let go=e.target.closest('[data-go]');if(go)navigate(go.dataset.go);let del=e.target.closest('[data-delete]');if(del){let [r,id]=del.dataset.delete.split(':');if(!confirm('Delete this record? This cannot be undone.'))return;api(r+'/'+id,{method:'DELETE'}).then(()=>{toast('Deleted');refresh()}).catch(err=>toast(err.message))}let deleteMeal=e.target.closest('[data-delete-recipe]');if(deleteMeal){deleteRecipeOption(Number(deleteMeal.dataset.deleteRecipe));return}let done=e.target.closest('[data-taskdone]');if(done)api('tasks/'+done.dataset.taskdone,{method:'PATCH',body:JSON.stringify({data:{status:'done'}})}).then(()=>{toast('Task completed');refresh()});let pay=e.target.closest('[data-pay]');if(pay)api('bills/'+pay.dataset.pay+'/pay',{method:'POST'}).then(()=>{toast('Bill marked paid');refresh()});let comp=e.target.closest('[data-complete]');if(comp)api('routines/'+comp.dataset.complete+'/complete',{method:'POST'}).then(()=>{toast('Routine completed');refresh()}).catch(err=>toast(err.message));let edit=e.target.closest('[data-edit-routine]');if(edit){const r=(cache.routines||[]).find(x=>x.id===Number(edit.dataset.editRoutine));if(r)openModal('routine',r)}let editRecord=e.target.closest('[data-edit-routine-record]');if(editRecord){const r=(cache.routines||[]).find(x=>x.id===Number(editRecord.dataset.editRoutineRecord));if(r)openModal('routine_record',{...r,completed_at:r.last_done||today})}let modal=e.target.closest('[data-modal]');if(modal)openModal(modal.dataset.modal)});
const configs={planner:{title:'Add planner item'},bill:{title:'Add bill',resource:'bills',fields:[['name','Bill name','text',true],['amount_mad','Amount (MAD)','number',true],['due_date','Due date','date',true],['reminder_days','Remind me before (days)','number'],['recurrence','Recurrence','select',['none','monthly','quarterly','yearly']],['notes','Notes','textarea']]},routine:{title:'Add routine',resource:'routines',fields:[['name','Routine name','text',true],['interval_days','Repeat every (days)','number',true],['routine_time','Routine time (24-hour)','time24'],['routine_late_time','Late reminder time (optional)','time24'],['category','Category','text'],['notes','Notes','textarea']]},routine_record:{title:'Edit last completion record',resource:'routines',fields:[['completed_at','Last completed on','date',true]]}};
function openModal(type,existing=null){
 delete $('#modal').dataset.lockBackdrop;
 const c=configs[type];$('#modal-title').textContent=existing?'Edit '+(existing.name||'routine'):c.title;
 if(type==='planner'){
  $('#modal-form').innerHTML=`<label>Type<select name="kind"><option value="task">Task / reminder</option><option value="event">Event / appointment</option></select></label><label>Title<input name="title" required maxlength="160" placeholder="What do you need to do?" /></label><label data-field="date">Date<input name="date" type="text" inputmode="numeric" value="${displayDateInput(selectedCalendarDate)}" placeholder="DD/MM/YY (optional for tasks)" autocomplete="off" /></label><label data-field="time">Time<input name="time" type="text" inputmode="numeric" placeholder="HH:MM (optional)" maxlength="5" pattern="(?:[01][0-9]|2[0-3]):[0-5][0-9]" autocomplete="off" /></label><label data-field="end_time" hidden>End time (event only)<input name="end_time" type="text" inputmode="numeric" placeholder="HH:MM (optional)" maxlength="5" pattern="(?:[01][0-9]|2[0-3]):[0-5][0-9]" autocomplete="off" /></label><label data-field="priority">Priority<select name="priority"><option>normal</option><option>high</option><option>low</option></select></label><label data-field="category">Category<input name="category" placeholder="general" /></label><label>Notes<textarea name="notes" placeholder="Optional details"></textarea></label><button class="primary">Save planner item</button>`;
  const kind=$('#modal-form [name="kind"]');const showFields=()=>{const isEvent=kind.value==='event';$('#modal-form [data-field="end_time"]').hidden=!isEvent;$('#modal-form [data-field="priority"]').hidden=isEvent;$('#modal-form [data-field="date"]').querySelector('input').placeholder=isEvent?'DD/MM/YY (required)':'DD/MM/YY (optional)';$('#modal-form [data-field="category"]').querySelector('input').placeholder=isEvent?'personal':'general'};kind.addEventListener('change',showFields);showFields();
  $('#modal-form').onsubmit=async e=>{e.preventDefault();try{const d=Object.fromEntries(new FormData(e.target));const day=d.date?parseDateInput(d.date):'';for(const field of ['time','end_time'])if(d[field]&&!/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(d[field]))throw Error('Enter time as 24-hour HH:MM.');let resource,payload;if(d.kind==='event'){if(!day)throw Error('Events need a date.');resource='events';payload={title:d.title.trim(),start_at:day+(d.time?'T'+d.time:''),end_at:d.end_time?day+'T'+d.end_time:null,category:d.category||'personal',notes:d.notes||''};if(payload.end_at&&payload.end_at<=payload.start_at)throw Error('End time must be after start time.')}else{resource='tasks';if(d.time&&!day)throw Error('Choose a date before setting a task reminder time.');payload={title:d.title.trim(),due_date:day||null,due_time:d.time||null,priority:d.priority||'normal',category:d.category||'general',notes:d.notes||''}}if(!payload.title)throw Error('Enter a title.');await api(resource,{method:'POST',body:JSON.stringify({data:payload})});$('#modal').classList.add('hidden');toast(d.kind==='event'?'Event saved':'Task saved');refresh()}catch(err){toast(err.message)}};
 }else if(type==='routine'){
  const lateValue=existing?.routine_late_time||'';
  const rolloverValue=existing?Number(existing.routine_rollover_after_late)===1:Boolean(lateValue);
  $('#modal-form').innerHTML=`<label>Routine name<input name="name" required maxlength="160" value="${esc(existing?.name||'')}" placeholder="e.g. Read 20 pages"></label><label>Repeat every (days)<input name="interval_days" type="number" min="1" max="365" step="1" required value="${Number(existing?.interval_days||7)}"></label><div class="two-field"><label>Routine time<input name="routine_time" type="text" inputmode="numeric" maxlength="5" pattern="(?:[01][0-9]|2[0-3]):[0-5][0-9]" value="${esc(existing?.routine_time||'10:00')}" placeholder="HH:MM"></label><label>Late reminder (optional)<input name="routine_late_time" type="text" inputmode="numeric" maxlength="5" pattern="(?:[01][0-9]|2[0-3]):[0-5][0-9]" value="${esc(lateValue)}" placeholder="Leave blank to disable"></label></div><div class="rollover-setting" id="routine-rollover-setting" ${lateValue?'':'hidden'}><label class="rollover-toggle"><input type="checkbox" name="routine_rollover_after_late" ${rolloverValue?'checked':''}><span><b>Move to tomorrow if still incomplete</b><small>After the late Telegram reminder, an unfinished routine shifts to tomorrow. This option is only available when a late reminder is set.</small></span></label></div><label>Category<input name="category" value="${esc(existing?.category||'personal')}" placeholder="personal"></label><label>Notes<textarea name="notes" placeholder="Optional">${esc(existing?.notes||'')}</textarea></label><button class="primary">${existing?'Save changes':'Save routine'}</button>`;
  const form=$('#modal-form'),lateInput=form.elements.routine_late_time,rolloverBox=$('#routine-rollover-setting'),rolloverInput=form.elements.routine_rollover_after_late;let rolloverTouched=false;
  const updateRolloverVisibility=()=>{const enabled=Boolean(lateInput.value.trim());rolloverBox.hidden=!enabled;if(!enabled)rolloverInput.checked=false;else if(!rolloverTouched&&!existing)rolloverInput.checked=true};
  lateInput.addEventListener('input',updateRolloverVisibility);rolloverInput.addEventListener('change',()=>{rolloverTouched=true});updateRolloverVisibility();
  form.onsubmit=async e=>{e.preventDefault();try{const d=Object.fromEntries(new FormData(form));d.interval_days=Number(d.interval_days);d.routine_late_time=d.routine_late_time.trim()||null;d.routine_rollover_after_late=d.routine_late_time&&rolloverInput.checked?1:0;if(!Number.isInteger(d.interval_days)||d.interval_days<1||d.interval_days>365)throw Error('Repeat interval must be between 1 and 365 days.');for(const field of ['routine_time','routine_late_time'])if(d[field]&&!/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(d[field]))throw Error('Enter time in 24-hour HH:MM format.');if(d.routine_time&&d.routine_late_time&&d.routine_late_time<=d.routine_time)throw Error('Late reminder must be later than routine time.');const path=existing?`routines/${existing.id}`:'routines';await api(path,{method:existing?'PATCH':'POST',body:JSON.stringify({data:d})});closeModal();toast(existing?'Routine updated':'Routine saved');refresh()}catch(err){toast(err.message)}};
 }else{
  $('#modal-form').innerHTML=c.fields.map(([n,l,t,req])=>{const v=existing?.[n]??(n==='reminder_days'?'3':n==='interval_days'?'7':n==='routine_time'?'10:00':n==='routine_late_time'?'':n==='due_date'||n==='expiry_date'?today:'');if(t==='textarea')return `<label>${l}<textarea name="${n}">${esc(v)}</textarea></label>`;if(t==='select')return `<label>${l}<select name="${n}">${req.map(x=>`<option ${String(v)===x?'selected':''}>${x}</option>`).join('')}</select></label>`;const inputType=(t==='date'||t==='datetime-local'||t==='time24')?'text':t;const value=t==='date'?(v?displayDateInput(v):''):t==='datetime-local'?displayDateTimeInput(v):v;const dateAttrs=t==='date'?'inputmode="numeric" placeholder="DD/MM/YY" autocomplete="off"':t==='datetime-local'?'placeholder="DD/MM/YY HH:MM" autocomplete="off"':t==='time24'?(n==='routine_late_time'?'inputmode="numeric" placeholder="Optional · leave blank to disable" maxlength="5" pattern="(?:[01][0-9]|2[0-3]):[0-5][0-9]" autocomplete="off"':'inputmode="numeric" placeholder="HH:MM (00:00–23:59)" maxlength="5" pattern="(?:[01][0-9]|2[0-3]):[0-5][0-9]" autocomplete="off"'):'';return `<label>${l}<input name="${n}" type="${inputType}" ${dateAttrs} ${req===true?'required':''} ${n==='interval_days'?'min="1" step="1"':''} ${n==='amount_mad'?'step="0.01" min="0"':''} value="${esc(value)}"></label>`}).join('')+'<button class="primary">'+(existing?'Save changes':'Save')+'</button>';
  $('#modal-form').onsubmit=async e=>{e.preventDefault();try{let d=Object.fromEntries(new FormData(e.target));for(const [n,l,t] of c.fields){if(t==='date'&&d[n])d[n]=parseDateInput(d[n]);if(t==='datetime-local'&&d[n])d[n]=parseDateTimeInput(d[n])}for(let k of ['amount_mad','reminder_days','interval_days'])if(d[k]!==undefined&&d[k]!=='')d[k]=Number(d[k]);for(const field of ['routine_time','routine_late_time'])if(d[field]&&!/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(d[field]))throw Error('Enter time in 24-hour HH:MM format, from 00:00 to 23:59.');if(type==='routine'&&d.routine_time&&d.routine_late_time&&d.routine_late_time<=d.routine_time)throw Error('Late reminder time must be later than the routine time.');const path=type==='routine_record'?`routines/${existing.id}/last-record`:existing?`${c.resource}/${existing.id}`:c.resource;await api(path,{method:existing?'PATCH':'POST',body:JSON.stringify({data:d})});$('#modal').classList.add('hidden');toast(existing?'Changes saved':'Saved successfully');refresh()}catch(err){toast(err.message)}}
 }
 $('#modal').classList.remove('hidden');
}
function closeModal(){if($('#modal').dataset.lockBackdrop==='true')delete $('#modal').dataset.lockBackdrop;$('#modal').classList.add('hidden')}
$('#modal-close').onclick=closeModal;$('#modal').onclick=e=>{if(e.target.id==='modal'&&$('#modal').dataset.lockBackdrop!=='true')closeModal()};
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&$('#modal').dataset.lockBackdrop==='true'){e.preventDefault();e.stopPropagation()}});
function bindForm(id,resource,transform=x=>x){$(id).addEventListener('submit',async e=>{e.preventDefault();try{let d=Object.fromEntries(new FormData(e.target));for(const [k,v] of Object.entries(d))if(['date','due_date','expiry_date','last_done','next_due'].includes(k)&&v)d[k]=parseDateInput(v);d=transform(d);await api(resource,{method:'POST',body:JSON.stringify({data:d})});e.target.reset();e.target.querySelectorAll('.date-entry').forEach(x=>x.value=displayDateInput(today));const successMessage=resource==='treadmill'?'Treadmill session saved':resource==='weights'?'Weight saved':'Saved successfully';toast(successMessage);refresh()}catch(err){toast(err.message)}})}
bindForm('#plan-form','workout_plan');bindForm('#weight-form','weights',d=>{const kg=Number(String(d.kg).replace(',','.'));if(!Number.isFinite(kg)||kg<=0)throw Error('Enter a valid weight.');return {...d,kg}});bindForm('#treadmill-form','treadmill',d=>({date:d.date,duration_min:Number(d.duration_min)}));bindForm('#workout-form','workouts',d=>({name:d.name,date:d.date,notes:d.notes}));

function recipeCard(r){
 const ingredients=Array.isArray(r.ingredients)?r.ingredients:[];
 const kcal=r.calories_kcal==null?'Kcal not set':`${Number(r.calories_kcal).toLocaleString()} kcal / plate`;
 const interval=Math.max(1,Number(r.eating_interval_days||1));
 const next=(cache.daily_meals||[]).filter(m=>Number(m.recipe_id)===Number(r.id)&&m.status==='pending'&&m.plan_date>=today).sort((a,b)=>a.plan_date.localeCompare(b.plan_date))[0];
 const home=r.meal_source!=='outside';
 const stock=Math.max(0,Number(r.plates_ready||0));
 const reminder=(cache.meal_cook_reminders||[]).find(x=>Number(x.recipe_id)===Number(r.id)&&x.status==='open');
 const excluded=(r.excluded_weekdays||[]).map(x=>WEEKDAY_LABELS[Number(x)]).filter(Boolean);
 const excludedLine=excluded.length?`<div class="meal-excluded-days">Not scheduled: ${excluded.map(esc).join(', ')}</div>`:'';
 const stockControl=home?`<div class="meal-stock-control"><div><small>Ready plates now</small><b>${stock}</b></div><label class="sr-only" for="meal-stock-${r.id}">Ready plates</label><input id="meal-stock-${r.id}" type="number" min="0" max="1000" step="1" value="${stock}" data-stock-input="${r.id}"><button class="mini-btn" data-stock-save="${r.id}">Update</button></div>`:`<div class="meal-outside-note">No kitchen stock tracking · eating outside</div>`;
 return `<article class="recipe-card ${home?'home-meal-card':'outside-meal-card'}"><div class="recipe-card-top"><span class="meal-routine-badge">${home?'🏠 Home':'🍴 Outside'} · Every ${interval} day${interval===1?'':'s'}</span><span class="recipe-plates">${home?`${stock} ready`:'No stock limit'}</span></div><div class="recipe-card-heading"><h3>${esc(r.name)}</h3><span class="meal-kcal">${esc(kcal)}</span></div>${stockControl}${excludedLine}<div class="recipe-ingredients"><b>Ingredients</b><div class="ingredient-chip-list">${ingredients.map(x=>`<span class="ingredient-chip">${esc(x)}</span>`).join('')||'<span class="muted">No ingredients saved</span>'}</div></div><div class="recipe-next-line">${reminder?`<span class="recipe-next-dot"></span><b>Cooking needed · ${formatDate(reminder.due_date)}</b>`:next?`<span class="recipe-next-dot"></span>Next plate · <b>${formatDate(next.plan_date)}</b>`:'<span class="recipe-next-dot muted-dot"></span>No plate currently scheduled'}</div><div class="recipe-card-actions"><button class="mini-btn" data-edit-recipe="${r.id}">Edit meal</button><button class="mini-btn danger" data-delete-recipe="${r.id}">Delete meal</button></div></article>`;
}
function renderMeals(){
 const recipes=cache.recipes||[];
 const home=recipes.filter(r=>r.meal_source!=='outside'), outside=recipes.filter(r=>r.meal_source==='outside');
 const ready=home.reduce((a,r)=>a+Math.max(0,Number(r.plates_ready||0)),0);
 const filtered=mealFilter==='all'?recipes:recipes.filter(r=>(r.meal_source||'home')===mealFilter);
 $('#meal-option-count').textContent=`${recipes.length} saved option${recipes.length===1?'':'s'}`;
 const shoppingLeft=(cache.shopping_items||[]).filter(x=>!Number(x.checked)).length;
 $('#meal-overview-strip').innerHTML=`<div class="meal-overview-stat"><span>Saved options</span><b>${recipes.length}</b></div><div class="meal-overview-stat"><span>Home plates ready</span><b>${ready}</b></div><div class="meal-overview-stat"><span>🛒 Shopping list items left to buy</span><b>${shoppingLeft}</b></div>`;
 $$('#meal-source-tabs [data-meal-filter]').forEach(b=>{const active=b.dataset.mealFilter===mealFilter;b.classList.toggle('active',active);b.setAttribute('aria-selected',String(active));b.textContent=b.dataset.mealFilter==='all'?`All options (${recipes.length})`:b.dataset.mealFilter==='home'?'🏠 Home meals':'🍴 Outside options'});
 const emptyText=mealFilter==='home'?['🏠','No home meals yet','Add meals you cook at home. They stay visible even when no plates are ready.','＋ Add home meal']:mealFilter==='outside'?['🍴','No outside meal options yet','Add dishes you can choose when you eat outside.','＋ Add outside option']:['🍽️','Your meal options will appear here','Add a home meal or an outside option. Saved options remain available even when home stock is zero.','＋ Add meal'];
 $('#meal-recipe-list').innerHTML=filtered.map(recipeCard).join('')||`<div class="meal-empty-state"><div class="meal-empty-icon">${emptyText[0]}</div><h3>${emptyText[1]}</h3><p>${emptyText[2]}</p><button class="primary" id="empty-add-meal" data-default-meal-source="${mealFilter==='outside'?'outside':'home'}">${emptyText[3]}</button></div>`;
 renderTodayMeal();
 renderWeeklyMealPlan();
}
function renderWeeklyMealPlan(){
 const host=$('#weekly-meal-plan');if(!host)return;
 const start=weekStartISO(today),end=addDaysISO(start,6);
 $('#meal-week-range').textContent=`${formatDate(start)} – ${formatDate(end)}`;
 const meals=cache.daily_meals||[],recipes=cache.recipes||[],skips=new Set((cache.meal_schedule_skips||[]).map(x=>x.plan_date));
 const days=Array.from({length:7},(_,i)=>addDaysISO(start,i));
 host.innerHTML=days.map(day=>{
  const meal=meals.find(x=>x.plan_date===day),skipped=skips.has(day),isPast=day<today,weekday=new Date(day+'T12:00:00').getDay(),weekdayIndex=(weekday+6)%7;
  const title=longDateLabel(day),isToday=day===today;
  const state=meal?.status==='eaten'?'<span class="weekly-state is-eaten">✓ Eaten</span>':meal?'<span class="weekly-state is-pending">Planned</span>':skipped?'<span class="weekly-state is-skipped">Day off</span>':'<span class="weekly-state is-empty">Not planned</span>';
  const name=meal?esc(meal.recipe_name||'Meal'):skipped?'No meal planned':'Choose what to eat';
  const meta=meal?`${meal.meal_source==='outside'?'🍴 Outside':'🏠 Home'}${meal.calories_kcal==null?'':` · ${Number(meal.calories_kcal)} kcal`}`:skipped?'You chose to keep this day free':'No plate assigned yet';
  let controls='';
  if(!isPast&&meal?.status!=='eaten'){
   const options=recipes.filter(r=>recipeCanBeAssignedForDay(r,day,meals));
   const optionHtml=options.map(r=>{
    const current=meal&&Number(meal.recipe_id)===Number(r.id);
    const reserved=meals.filter(m=>m.status==='pending'&&Number(m.recipe_id)===Number(r.id)&&m.plan_date>=today&&m.plan_date!==day).length;
    const free=Math.max(0,Number(r.plates_ready||0)-reserved);
    const stockText=r.meal_source==='outside'?'Outside':free>0?`${free} free`:'move later reservation';
    return `<option value="${Number(r.id)}" ${current?'selected':''}>${esc(r.name)} · ${stockText}</option>`;
   }).join('');
   const selector=options.length?`<div class="weekly-meal-editor"><label class="sr-only" for="weekly-meal-${day}">Select meal for ${esc(title)}</label><select id="weekly-meal-${day}" data-week-meal-select="${day}"><option value="">Choose a meal…</option>${optionHtml}</select><button class="mini-btn weekly-save-btn" data-week-assign="${day}">${meal?'Change':'Add meal'}</button></div>`:`<small class="weekly-no-options">No eligible meals. Add an option or update Home stock.</small>`;
   const skipButton=skipped?`<button class="mini-btn" data-week-restore="${day}">Restore day</button>`:`<button class="mini-btn weekly-skip-btn" data-week-skip="${day}">Skip day</button>`;
   controls=`<div class="weekly-meal-controls">${selector}${skipButton}</div>`;
  }
  return `<article class="weekly-day-card ${isToday?'is-today':''} ${isPast?'is-past':''} ${skipped?'is-day-off':''}"><div class="weekly-day-top"><b>${esc(title)}</b>${isToday?'<span class="weekly-today-label">Today</span>':''}</div><div class="weekly-day-content"><div class="weekly-meal-name">${name}</div><div class="weekly-meal-meta">${esc(meta)}</div></div><div class="weekly-day-bottom">${state}${controls}</div></article>`;
 }).join('');
}

function renderPlannerShopping(){
 const shopping=cache.shopping_items||[];
 const open=shopping.filter(x=>!Number(x.checked)), bought=shopping.filter(x=>Number(x.checked));
 const count=shopping.length, progress=count?Math.round(bought.length/count*100):0;
 $('#meals-shopping-summary').innerHTML=`<div class="shopping-progress-copy"><b>${open.length} left to buy</b><span>${bought.length} of ${count} completed</span></div><div class="shopping-progress-track"><span style="width:${progress}%"></span></div>`;
 const row=(i)=>`<div class="shopping-row ${Number(i.checked)?'is-checked':''}"><label><input type="checkbox" data-shopping-check="${i.id}" ${Number(i.checked)?'checked':''}><span>${esc(i.name)}</span></label><button class="mini-btn shopping-delete" data-delete="shopping_items:${i.id}" aria-label="Remove ${esc(i.name)}">×</button></div>`;
 const openHtml=open.map(row).join(''), boughtHtml=bought.map(row).join('');
 $('#meals-shopping-list').innerHTML=(openHtml?`<div class="shopping-group"><div class="shopping-group-title"><span class="shopping-group-dot"></span>To buy <b>${open.length}</b></div>${openHtml}</div>`:'')+(boughtHtml?`<div class="shopping-group purchased-group"><div class="shopping-group-title"><span class="shopping-group-dot bought"></span>Purchased <b>${bought.length}</b></div>${boughtHtml}</div>`:'')||`<div class="shopping-empty"><span>🛒</span><b>Your shopping list is clear</b><small>Add items here or collect ingredients from your upcoming meal routines.</small></div>`;
}
function renderTodayMeal(){
 const hosts=[$('#today-meal-card'),$('#meals-today-meal-card')].filter(Boolean);
 if(!hosts.length)return;
 const meal=(cache.daily_meals||[]).find(x=>x.plan_date===today);
 const meals=cache.daily_meals||[];
 const choices=(cache.recipes||[]).filter(r=>recipeCanBeAssignedForDay(r,today,meals));
 const pickerMarkup=`<div class="today-meal-empty"><div class="today-meal-empty-copy"><b>No plate selected for today</b><p class="muted">Choose a Home meal with ready plates or an Outside option. When all Home plates are reserved later in the plan, choosing one moves the latest automatic reservation to today; your manual choices are protected.</p></div><div class="today-meal-picker"><label>Select today's meal</label><select data-today-meal-choice aria-label="Select today's meal" ${choices.length?'':'disabled'}><option value="">${choices.length?'Choose a meal…':'No meals currently available'}</option>${choices.map(r=>{const reserved=meals.filter(m=>m.status==='pending'&&Number(m.recipe_id)===Number(r.id)&&m.plan_date>=today).length,free=Math.max(0,Number(r.plates_ready||0)-reserved),stock=r.meal_source==='outside'?'Outside':free>0?`${free} ready`:'move later reservation';return `<option value="${Number(r.id)}">${esc(r.name)} · ${stock}</option>`}).join('')}</select><button type="button" class="primary" data-select-today-meal ${choices.length?'':'disabled'}>Choose this meal</button></div>${choices.length?'':'<small class="muted">Add an Outside meal or update the stock of a Home meal in the meal options below.</small>'}</div>`;
 const markup=!meal
  ? null
  : (()=>{
    const kcal=meal.calories_kcal==null?'Calories not set':`${Number(meal.calories_kcal)} kcal per plate`;
    const state=meal.status==='eaten'?'<span class="meal-confirmed">✓ Confirmed eaten</span>':'<span class="meal-pending">Waiting for your 22:00 Telegram check-in</span>';
    return `<div class="today-meal-main ${meal.status==='eaten'?'is-eaten':'is-pending'}"><div><h3>${esc(meal.recipe_name||'Meal')}</h3><p class="muted">${esc(kcal)}</p>${state}</div><div class="today-meal-actions">${meal.status==='pending'?`<button class="mini-btn success" data-meal-confirm="${meal.id}" data-meal-decision="yes">I ate it</button><button class="mini-btn" data-meal-confirm="${meal.id}" data-meal-decision="no">Move to tomorrow</button><button class="mini-btn danger" data-remove-plate="${meal.id}" aria-label="Remove this pending plate">Remove plate</button>`:'<span class="tag good">Done</span>'}</div></div>`;
  })();
 hosts.forEach(host=>{
  const isMealsPage=host.id==='meals-today-meal-card';
  if(!meal){
   host.innerHTML=isMealsPage?pickerMarkup:'<div class="today-meal-empty today-meal-empty-overview"><div class="today-meal-empty-copy"><b>No plate selected for today</b><p class="muted">Choose today&#39;s meal from the Meals page.</p></div><button type="button" class="secondary" data-go="meals">Choose a meal ↗</button></div>';
  }else host.innerHTML=markup;
  host.classList.toggle('has-meal',Boolean(meal));
 });
}
async function removePlate(id){
 if(!confirm('Remove this pending plate from the schedule? It will not be marked as eaten and your ready-plate stock will stay unchanged.'))return;
 try{await api(`daily_meals/${id}`,{method:'DELETE'});toast('Plate removed from the schedule');refresh()}catch(err){toast(err.message)}
}
async function deleteRecipeOption(id){
 const recipe=(cache.recipes||[]).find(r=>Number(r.id)===id);
 if(!recipe)return;
 if(!confirm(`Remove “${recipe.name}” from your meal options? Pending plates will be cancelled. Any confirmed eaten history will be kept.`))return;
 try{const result=await api(`recipes/${id}`,{method:'DELETE'});toast(result.history_preserved?'Meal option removed; eaten history kept':'Meal option deleted');refresh()}catch(err){toast(err.message)}
}

function parseMealIngredients(text){
 const list=String(text||'').split(/[\n,;]+/).map(x=>x.trim().replace(/^[-•]\s*/,'' )).filter(Boolean);
 const seen=new Set();return list.filter(x=>{const k=x.toLowerCase().replace(/\s+/g,' ');if(seen.has(k))return false;seen.add(k);return true;});
}
function openRecipeModal(existing=null,defaultSource='home'){
 $('#modal').dataset.lockBackdrop='true';
 const form=$('#modal-form');$('#modal-title').textContent=existing?'Edit meal':'Add a meal';
 const ingredients=Array.isArray(existing?.ingredients)?existing.ingredients.join('\n'):'';
 const source=existing?.meal_source||defaultSource||'home';
 const excludedDays=new Set((existing?.excluded_weekdays||[]).map(Number));
 const weekdayFields=WEEKDAY_LABELS.map((name,index)=>`<label class="weekday-exclude-option"><input type="checkbox" name="excluded_weekdays" value="${index}" ${excludedDays.has(index)?'checked':''}><span>${name}</span></label>`).join('');
 form.innerHTML=`<label>Meal type<select name="meal_source"><option value="home" ${source==='home'?'selected':''}>Home meal</option><option value="outside" ${source==='outside'?'selected':''}>Outside meal</option></select></label><label>Meal name<input name="name" required maxlength="160" value="${esc(existing?.name||'')}" placeholder="e.g. Chicken tagine"></label><label class="meal-kcal-field"><span>Kcal per plate</span><input name="calories_kcal" type="number" min="0" max="10000" step="1" value="${existing?.calories_kcal??''}" placeholder="e.g. 650"></label><div class="two-field"><label>Eat every (days)<input name="eating_interval_days" type="number" min="1" max="90" step="1" required value="${Number(existing?.eating_interval_days||1)}"><small class="muted">Choosing this meal again restarts its repeat interval from that day.</small></label><label class="home-stock-field">Plates ready now<input name="plates_ready" type="number" min="0" max="1000" step="1" value="${Number(existing?.plates_ready??existing?.batch_plates??1)}"></label></div><fieldset class="weekday-exclude-field"><legend>Don't schedule this meal on</legend><p>Use this for restaurants that close on certain days, such as Sunday.</p><div class="weekday-exclude-grid">${weekdayFields}</div></fieldset><label class="meal-ingredients-field"><span>Ingredients · one item per line</span><textarea name="ingredients" rows="5" placeholder="Chicken\nOnions\nTomatoes\nOlive oil">${esc(ingredients)}</textarea></label><button class="primary">${existing?'Save changes':'Save meal'}</button>`;
 const sourceSelect=form.elements.meal_source,stockField=form.querySelector('.home-stock-field'),ingredientsInput=form.elements.ingredients,ingredientsLabel=form.querySelector('.meal-ingredients-field span'),kcalLabel=form.querySelector('.meal-kcal-field span');
 const syncSource=()=>{const outside=sourceSelect.value==='outside';stockField.hidden=outside;ingredientsInput.required=!outside;ingredientsLabel.textContent=outside?'Ingredients (optional) · one item per line':'Ingredients · one item per line';kcalLabel.textContent=outside?'Kcal per plate (optional)':'Kcal per plate'};sourceSelect.addEventListener('change',syncSource);syncSource();
 form.onsubmit=async e=>{e.preventDefault();try{const f=new FormData(form);const names=parseMealIngredients(f.get('ingredients'));const outside=sourceSelect.value==='outside';if(!outside&&!names.length)throw Error('Add at least one ingredient for a Home meal.');const rawKcal=String(f.get('calories_kcal')||'').trim();const payload={name:String(f.get('name')||'').trim(),meal_source:String(f.get('meal_source')),calories_kcal:rawKcal?Number(rawKcal):null,eating_interval_days:Number(f.get('eating_interval_days')),plates_ready:sourceSelect.value==='home'?Number(f.get('plates_ready')):0,batch_plates:sourceSelect.value==='home'?Math.max(1,Number(f.get('plates_ready'))):1,ingredients_json:JSON.stringify(names),excluded_weekdays_json:JSON.stringify(f.getAll('excluded_weekdays').map(Number))};if(!payload.name)throw Error('Meal name is required.');if(!Number.isInteger(payload.eating_interval_days)||payload.eating_interval_days<1||payload.eating_interval_days>90)throw Error('Eating interval must be between 1 and 90 days.');if(sourceSelect.value==='home'&&(!Number.isInteger(payload.plates_ready)||payload.plates_ready<0||payload.plates_ready>1000))throw Error('Ready plates must be a whole number from 0 to 1000.');if(payload.calories_kcal!==null&&(!Number.isFinite(payload.calories_kcal)||payload.calories_kcal<0||payload.calories_kcal>10000))throw Error('Enter valid kcal per plate.');await api(existing?`recipes/${existing.id}`:'recipes',{method:existing?'PATCH':'POST',body:JSON.stringify({data:payload})});closeModal();toast(existing?'Meal updated':'Meal saved');refresh()}catch(err){toast(err.message)}};
 $('#modal').classList.remove('hidden');
}
$('#add-recipe-btn').addEventListener('click',()=>openRecipeModal());
$('#meal-source-tabs').addEventListener('click',e=>{const b=e.target.closest('[data-meal-filter]');if(!b)return;mealFilter=b.dataset.mealFilter;renderMeals()});
document.addEventListener('click',e=>{const emptyBtn=e.target.closest('#empty-add-meal');if(emptyBtn)openRecipeModal(null,emptyBtn.dataset.defaultMealSource||'home')});
async function buildShoppingList(){try{const future=(cache.daily_meals||[]).filter(x=>x.plan_date>=today).map(x=>x.plan_date).sort();if(!future.length)throw Error('No upcoming meal ingredients are available yet.');const data=await api('meals/shopping-list',{method:'POST',body:JSON.stringify({data:{start_date:today,end_date:future[future.length-1]}})});toast(`${data.new_items} item(s) added to shopping list`);refresh()}catch(err){toast(err.message)}}
$('#meals-shopping-from-plan').addEventListener('click',buildShoppingList);
document.addEventListener('click',async e=>{const btn=e.target.closest('[data-stock-save]');if(!btn)return;const id=Number(btn.dataset.stockSave),input=document.querySelector(`[data-stock-input="${id}"]`);const qty=Number(input?.value);if(!Number.isInteger(qty)||qty<0||qty>1000){toast('Enter a whole number from 0 to 1000.');return}try{await api(`recipes/${id}/stock`,{method:'POST',body:JSON.stringify({data:{plates_ready:qty}})});toast('Ready plates updated');refresh()}catch(err){toast(err.message)}});
$('#meals-shopping-add-form').addEventListener('submit',async e=>{e.preventDefault();try{const f=new FormData(e.target);await api('shopping_items',{method:'POST',body:JSON.stringify({data:{name:f.get('name'),source:'manual'}})});e.target.reset();toast('Item added');refresh()}catch(err){toast(err.message)}});
$('#meals-shopping-list').addEventListener('change',async e=>{const input=e.target.closest('[data-shopping-check]');if(!input)return;try{await api(`shopping_items/${input.dataset.shoppingCheck}`,{method:'PATCH',body:JSON.stringify({data:{checked:input.checked?1:0}})});refresh()}catch(err){toast(err.message)}});
$('#clear-bought-items').addEventListener('click',async()=>{const bought=(cache.shopping_items||[]).filter(i=>Number(i.checked));if(!bought.length){toast('No purchased items to clear');return}if(!confirm(`Remove ${bought.length} purchased item(s) from the list?`))return;try{await Promise.all(bought.map(i=>api(`shopping_items/${i.id}`,{method:'DELETE'})));toast('Purchased items cleared');refresh()}catch(err){toast(err.message)}});
async function confirmMeal(id,decision){try{await api(`daily-meals/${id}/confirm`,{method:'POST',body:JSON.stringify({data:{decision}})});toast(decision==='yes'?'Plate confirmed':'Plate and following days moved');refresh()}catch(err){toast(err.message)}}
document.addEventListener('click',async e=>{
 const resetWeek=e.target.closest('[data-reset-week-plan]');
 if(resetWeek){if(!confirm('Generate a fresh upcoming plan? This replaces pending plates from today onward and clears future day-off markers. Past history and confirmed eaten plates stay; unconfirmed overdue plates are included after the normal rollover.'))return;resetWeek.disabled=true;try{const result=await api('daily-meals/reset-week',{method:'POST',body:JSON.stringify({data:{}})});toast(`Upcoming plan refreshed · ${result.created} new plate(s)`);await refresh()}catch(err){toast(err.message)}finally{resetWeek.disabled=false}return}
 const weekAssign=e.target.closest('[data-week-assign]');
 if(weekAssign){const day=weekAssign.dataset.weekAssign,select=document.querySelector(`[data-week-meal-select="${day}"]`),recipeId=Number(select?.value),recipe=(cache.recipes||[]).find(r=>Number(r.id)===recipeId);if(!recipeId){toast('Choose a meal for this day first.');return}const movable=recipe?movableHomeReservation(recipe,day):null;if(movable&&!confirm(`A plate of ${recipe.name} is currently reserved for ${formatDate(movable.plan_date)}. Move that automatic reservation to ${formatDate(day)}?`))return;weekAssign.disabled=true;try{const result=await api('daily-meals/assign',{method:'POST',body:JSON.stringify({data:{plan_date:day,recipe_id:recipeId}})});const repeat=result.next_planned_date?` · next repeat ${formatDate(result.next_planned_date)}`:'';toast((result.reallocated_from?`Meal saved · moved reservation from ${formatDate(result.reallocated_from)}`:`Meal saved for ${formatDate(day)}`)+repeat);await refresh()}catch(err){toast(err.message)}finally{weekAssign.disabled=false}return}
 const weekSkip=e.target.closest('[data-week-skip]');
 if(weekSkip){const day=weekSkip.dataset.weekSkip,meal=(cache.daily_meals||[]).find(x=>x.plan_date===day);if(!confirm(meal?`Cancel the pending ${meal.recipe_name} plate for ${formatDate(day)} and leave this day free? Ready-plate stock will not be consumed.`:`Leave ${formatDate(day)} free of a meal?`))return;weekSkip.disabled=true;try{await api('daily-meals/assign',{method:'POST',body:JSON.stringify({data:{plan_date:day,recipe_id:null}})});toast('Day left free');await refresh()}catch(err){toast(err.message)}finally{weekSkip.disabled=false}return}
 const weekRestore=e.target.closest('[data-week-restore]');
 if(weekRestore){const day=weekRestore.dataset.weekRestore;weekRestore.disabled=true;try{await api('daily-meals/restore',{method:'POST',body:JSON.stringify({data:{plan_date:day}})});toast('Day restored to automatic planning');await refresh()}catch(err){toast(err.message)}finally{weekRestore.disabled=false}return}
 const choose=e.target.closest('[data-select-today-meal]');if(choose){const host=choose.closest('.today-meal-card'),select=host?.querySelector('[data-today-meal-choice]'),recipeId=Number(select?.value),recipe=(cache.recipes||[]).find(r=>Number(r.id)===recipeId);if(!recipeId){toast('Choose a meal first.');return}const movable=recipe?movableHomeReservation(recipe,today):null;if(movable&&!confirm(`A plate of ${recipe.name} is currently reserved for ${formatDate(movable.plan_date)}. Use it today and move that reservation?`))return;choose.disabled=true;try{const result=await api('daily-meals/select',{method:'POST',body:JSON.stringify({data:{recipe_id:recipeId,plan_date:today}})});const repeat=result.next_planned_date?` · next repeat ${formatDate(result.next_planned_date)}`:'';toast((result.reallocated_from?`Today's plate selected · moved a reservation from ${formatDate(result.reallocated_from)}`:`Today's plate selected · ${result.name}`)+repeat);await refresh()}catch(err){toast(err.message)}finally{choose.disabled=false}return}
 const edit=e.target.closest('[data-edit-recipe]');if(edit){const recipe=(cache.recipes||[]).find(r=>r.id===Number(edit.dataset.editRecipe));if(recipe)openRecipeModal(recipe)}const mealConfirmBtn=e.target.closest('[data-meal-confirm]');if(mealConfirmBtn)confirmMeal(Number(mealConfirmBtn.dataset.mealConfirm),mealConfirmBtn.dataset.mealDecision);const remove=e.target.closest('[data-remove-plate]');if(remove)removePlate(Number(remove.dataset.removePlate))
});
$$('.date-entry').forEach(x=>{if(!x.value)x.value=displayDateInput(today)});$('#treadmill-form [name="duration_min"]').addEventListener('input',updateTreadmillPreview);document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh()});refresh();setInterval(refresh,60000);
