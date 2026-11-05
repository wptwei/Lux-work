const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
(async()=>{
 const source=fs.readFileSync(require('node:path').join(__dirname,'../frontend/voice_input.js'),'utf8');
 const listeners={},windowListeners={},nodes=[],messages=[],intervals=[],timers=[],urls=new Map();let ctor,tracksStopped=0;
 const on=map=>(e,f)=>((map[e] ||= []).push(f));
 const fire=(map,e,arg)=>(map[e]||[]).slice().forEach(f=>f(arg));
 class Area {
   constructor(transport=false,selector=''){this.transport=transport;this.selector=selector;this.v='';this.selectionStart=0;this.selectionEnd=0;}
   set value(v){this.v=v} get value(){return this.v}
   matches(sel){return this.selector===sel}
   dispatchEvent(event){assert.equal(event.type,'input');if(this.transport)messages.push(JSON.parse(this.value))}
 }
 const input=new Area(true,'#voice-pcm-channel textarea'),composer=new Area(false,'#task-composer textarea');
 const status={textContent:''},preview={textContent:''},button={disabled:false,textContent:'',classList:{toggle(){}},setAttribute(){}}; composer.value='原有草稿';
 const elements={'#voice-pcm-channel textarea':input,'#voice-live-status':status,'#voice-live-preview':preview,'#voice-inline':{hidden:true},'#task-voice':button,'#task-composer textarea':composer};
 const meter={hidden:true,style:{value:'0',setProperty(name,value){this.value=value}}}; elements['#voice-level']=meter;
 const document={documentElement:{dataset:{}},hidden:false,querySelector:s=>elements[s],addEventListener:on(listeners)};
 const window={isSecureContext:true,addEventListener:on(windowListeners)};
 let context;
 class Processor {constructor(){this.port={onmessage:null,postMessage:data=>this.host?.port.onmessage?.({data})}}}
 class Worklet {constructor(){this.processor=new ctor();this.processor.host=this;this.port={onmessage:null,postMessage:data=>this.processor.port.onmessage({data})};nodes.push(this)}connect(){}disconnect(){}}
 class AudioContext {constructor(){this.sampleRate=48000;this.state='running';this.destination={};this.audioWorklet={addModule:async u=>vm.runInContext('(function(){'+await urls.get(u).text()+'})()',context)}}
 createMediaStreamSource(){return {connect(){},disconnect(){}}}createGain(){return {gain:{},connect(){},disconnect(){}}}async resume(){}async close(){this.state='closed'}}
 context=vm.createContext({document,window,navigator:{mediaDevices:{getUserMedia:async()=>({getTracks:()=>[{stop(){tracksStopped++}}]})}},HTMLTextAreaElement:Area,Event:class{constructor(type){this.type=type}},AudioContext,AudioWorkletNode:Worklet,AudioWorkletProcessor:Processor,registerProcessor:(name,c)=>ctor=c,Blob,URL:{createObjectURL:b=>{urls.set('blob:test',b);return 'blob:test'},revokeObjectURL(){}},crypto:{randomUUID:()=>`recording-${nodes.length}`},setInterval:f=>{intervals.push(f);return intervals.length},clearInterval(){},setTimeout:(fn,ms)=>{const id=setTimeout(fn,ms);timers.push({fn,ms,id});return id;},clearTimeout,Float32Array,Uint8Array,DataView,btoa:s=>Buffer.from(s,'binary').toString('base64')});
 vm.runInContext('('+source+')()',context);
 let lastClickEvent=null;
 const click=id=>{lastClickEvent={target:{closest:s=>s.split(', ').includes('#'+id)?{}:null},preventDefault(){this.defaultPrevented=true},stopImmediatePropagation(){this.stopped=true}};fire(listeners,'click',lastClickEvent);return lastClickEvent;};
 const tick=()=>new Promise(setImmediate);
 const deliver=value=>fire(windowListeners,'lux-voice-result',{detail:JSON.stringify(value)});
 const userEdit=(el,start,end,text)=>{el.selectionStart=start;el.selectionEnd=end;fire(listeners,'beforeinput',{target:el});const v=el.value;el.value=v.slice(0,start)+text+v.slice(end);el.selectionStart=el.selectionEnd=start+text.length;fire(listeners,'input',{target:el});};

 // ---- Scenario 1: partial writes live text into the composer, final corrects once ----
 click('task-voice');for(let i=0;i<6;i++)await tick();
 assert.equal(nodes.length,1);assert.equal(button.textContent,'结束说话');assert.equal(button.disabled,false);
 const worklet=nodes[0].processor;
 worklet.process([[new Float32Array(96000).fill(.25)]]);intervals[0]();
 assert.equal(meter.hidden,false);assert.ok(Number(meter.style.value)>0);
 assert.equal(messages[0].kind,'start');assert.equal(Buffer.from(messages[1].pcm,'base64').length,192000);
 worklet.process([[new Float32Array(128).fill(.5)]]);
 click('task-voice');for(let i=0;i<6;i++)await tick();
 const last=messages.at(-1);assert.equal(last.final,true);assert.equal(last.seq,2);
 assert.equal(Buffer.from(last.pcm,'base64').length,256);
 assert.equal(meter.hidden,true);assert.equal(meter.style.value,'0');
 assert.equal(tracksStopped,1);assert.equal(button.disabled,true);assert.equal(button.textContent,'转写中…');
 assert.equal(worklet.process([[new Float32Array(128)]]),false);
 const id=messages[0].id;
 deliver({id,kind:'ack',received_seq:2,seconds:2,phase:'final'});
 assert.match(status.textContent,/已收到 2 秒录音/);
 deliver({id,revision:1,ok:true,text:'识别中的文字',final:false,max_chars:20000});
 assert.equal(composer.value,'原有草稿\n识别中的文字'); assert.equal(preview.textContent,'');
 deliver({id,kind:'ack',received_seq:1,seconds:1,phase:'recording'});
 assert.equal(composer.value,'原有草稿\n识别中的文字');
 const final={id,revision:2,ok:true,text:'最终语音文字',final:true,max_chars:20000};
 deliver(final);
 assert.equal(composer.value,'原有草稿\n最终语音文字');
 assert.equal(button.disabled,false);assert.equal(preview.textContent,'');
 deliver(final);assert.equal(composer.value.split('最终语音文字').length,2);

 // ---- Scenario 2: manual edit inside the live region stops auto-replace ----
 composer.value='原有草稿';
 click('task-voice');for(let i=0;i<6;i++)await tick();
 const s2=messages.at(-1).id;
 nodes[1].processor.process([[new Float32Array(128).fill(.5)]]);
 deliver({id:s2,revision:1,ok:true,text:'识别中的文字',final:false,max_chars:20000});
 assert.equal(composer.value,'原有草稿\n识别中的文字');
 userEdit(composer,5,5,'改');
 assert.equal(composer.value,'原有草稿\n改识别中的文字');
 assert.match(status.textContent,/手动编辑/);
 deliver({id:s2,revision:2,ok:true,text:'改后的识别',final:false,max_chars:20000});
 assert.equal(composer.value,'原有草稿\n改识别中的文字'); assert.equal(preview.textContent,'改后的识别');
 click('task-voice');for(let i=0;i<6;i++)await tick();
 deliver({id:s2,revision:3,ok:true,text:'最终语音文字',final:true,max_chars:20000});
 assert.equal(composer.value,'原有草稿\n改识别中的文字'); assert.equal(preview.textContent,'最终语音文字');

 // ---- Scenario 3: manual input before the region is preserved and the region shifts ----
 composer.value='原有草稿';
 click('task-voice');for(let i=0;i<6;i++)await tick();
 const s3=messages.at(-1).id;
 nodes[2].processor.process([[new Float32Array(128).fill(.5)]]);
 deliver({id:s3,revision:1,ok:true,text:'识别中的文字',final:false,max_chars:20000});
 userEdit(composer,0,0,'前缀');
 assert.equal(composer.value,'前缀原有草稿\n识别中的文字');
 click('task-voice');for(let i=0;i<6;i++)await tick();
 deliver({id:s3,revision:2,ok:true,text:'最终语音文字',final:true,max_chars:20000});
 assert.equal(composer.value,'前缀原有草稿\n最终语音文字');

 // ---- Scenario 4: blocked final rolls back the managed region ----
 composer.value='原有草稿';
 click('task-voice');for(let i=0;i<6;i++)await tick();
 const s4=messages.at(-1).id;
 nodes[3].processor.process([[new Float32Array(128).fill(.5)]]);
 deliver({id:s4,revision:1,ok:true,text:'识别中的文字',final:false,max_chars:20000});
 click('task-voice');for(let i=0;i<6;i++)await tick();
 deliver({id:s4,revision:2,ok:false,text:'',message:'语音未通过检查',blocked:true,final:true,max_chars:20000});
 assert.equal(composer.value,'原有草稿'); assert.equal(preview.textContent,'');

 // ---- Scenario 5: cancel removes the managed region and keeps the original draft ----
 composer.value='原有草稿';
 click('task-voice');for(let i=0;i<6;i++)await tick();
 const s5=messages.at(-1).id, stoppedBeforeCancel=tracksStopped;
 nodes[4].processor.process([[new Float32Array(128).fill(.5)]]);
 deliver({id:s5,revision:1,ok:true,text:'识别中的文字',final:false,max_chars:20000});
 assert.equal(composer.value,'原有草稿\n识别中的文字');
 click('voice-cancel');await tick();
 assert.equal(tracksStopped,stoppedBeforeCancel+1);
 assert.match(status.textContent,/已取消录音/);
 assert.equal(composer.value,'原有草稿');
 deliver({id:s5,revision:99,ok:true,text:'取消后迟到',final:true,max_chars:20000});
 assert.equal(composer.value,'原有草稿');

 // ---- Scenario 6: send is blocked while recording ----
 composer.value='原有草稿';
 click('task-voice');for(let i=0;i<6;i++)await tick();
 nodes[5].processor.process([[new Float32Array(128).fill(.5)]]);
 const s6=messages.at(-1).id;
 deliver({id:s6,revision:1,ok:true,text:'识别中的文字',final:false,max_chars:20000});
 const sendEvt=click('task-send');
 assert.equal(sendEvt.stopped,true);assert.equal(sendEvt.defaultPrevented,true);
 click('task-voice');for(let i=0;i<6;i++)await tick();
 deliver({id:s6,revision:2,ok:true,text:'最终语音文字',final:true,max_chars:20000});

 // ---- Scenario 7: overflow keeps the draft and leaves text available for copying ----
 composer.value='原有草稿';
 click('task-voice');for(let i=0;i<6;i++)await tick();
 const s7=messages.at(-1).id;
 nodes[6].processor.process([[new Float32Array(128).fill(.5)]]);
 deliver({id:s7,revision:1,ok:true,text:'识别中的文字',final:false,max_chars:20000});
 click('task-voice');for(let i=0;i<6;i++)await tick();
 deliver({id:s7,revision:2,ok:true,text:'保留以便复制',final:true,max_chars:1});
 assert.equal(composer.value,'原有草稿'); assert.equal(preview.textContent,'保留以便复制');

 // ---- Scenario 8: server error releases the microphone and never inserts text ----
 composer.value='原有草稿';
 click('task-voice');for(let i=0;i<6;i++)await tick();
 const s8=messages.at(-1).id, stoppedBeforeError=tracksStopped;
 deliver({id:s8,error:'当前账号无权使用语音输入。'});
 await tick();
 assert.equal(tracksStopped,stoppedBeforeError+1);assert.equal(button.disabled,false);
 assert.equal(composer.value,'原有草稿');assert.equal(preview.textContent,'');

 // ---- Scenario 9: missing acknowledgements fail early and release the microphone ----
 click('task-voice');for(let i=0;i<6;i++)await tick();
 const missed=messages.at(-1).id;
 const ackTimeout=timers.filter(t=>t.ms===15000).at(-1);
 const stoppedBeforeTimeout=tracksStopped;
 clearTimeout(ackTimeout.id);ackTimeout.fn();await tick();
 assert.equal(tracksStopped,stoppedBeforeTimeout+1);
 assert.match(status.textContent,/15 秒内未收到后端回执/);
 deliver({id:missed,revision:1,ok:true,text:'超时晚到',final:true,max_chars:20000});

 // ---- Scenario 10: final acknowledgement distinguishes slow inference from missing transport ----
 click('task-voice');for(let i=0;i<6;i++)await tick();
 const slow=messages.at(-1).id;
 deliver({id:slow,kind:'ack',received_seq:0,seconds:0,phase:'recording'});
 click('task-voice');for(let i=0;i<6;i++)await tick();
 deliver({id:slow,kind:'ack',received_seq:1,seconds:0,phase:'final'});
 const finalTimeout=timers.filter(t=>t.ms===90000).at(-1);
 clearTimeout(finalTimeout.id);finalTimeout.fn();
 assert.match(status.textContent,/后端已收到完整录音/);
 assert.equal(button.disabled,false);

 console.log(JSON.stringify({status:'PASS',single_button_toggle:true,live_partial_written_to_composer:true,partial_region_replaced_once:true,manual_edit_stops_autoreplace:true,edit_before_region_preserved:true,blocked_final_rolls_back_region:true,cancel_removes_region:true,send_blocked_while_recording:true,overflow_keeps_draft:true,final_tail_samples:128,microphone_tracks_released:true,stale_session_ignored:true,ack_does_not_overwrite_transcript:true,missing_ack_stops_microphone:true,timeout_distinguishes_receive_and_inference:true,method:'Node VM with simulated Web Audio; not a physical microphone test'},null,2));
})().catch(e=>{console.error(e);process.exit(1)});
