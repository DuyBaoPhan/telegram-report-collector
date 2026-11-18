// Deterministic Node VM tests: no browser, network, production DB, or dependencies.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const timers = new Map(); let nextTimer=0, active=0, maxActive=0;
const listeners={}, elements=new Map(), calls=[];
let token='initial', failure=false, hold=null;
function element(){return {value:'all',dataset:{},classList:{add(){},remove(){}},addEventListener(){},replaceChildren(){},append(){},style:{}};}
const document={hidden:false,body:element(),documentElement:element(),getElementById(id){if(!elements.has(id))elements.set(id,element());return elements.get(id);},addEventListener(name,fn){listeners[name]=fn;},querySelectorAll(){return [];}};
const context=vm.createContext({document,window:{addEventListener(name,fn){listeners[name]=fn;}},localStorage:{getItem(){return null;}},Intl,URLSearchParams,AbortController,console,
setTimeout(fn,delay){const id=++nextTimer;timers.set(id,{fn,delay});return id;},clearTimeout(id){timers.delete(id);},
async fetch(path){calls.push(path);active++;maxActive=Math.max(maxActive,active);try{if(hold)await hold;if(failure)throw Error('offline');return {ok:true,async json(){return path==='/api/changes'?{token}:{};}};}finally{active--;}}});
let source=fs.readFileSync('app/dashboard_static/dashboard.js','utf8');
source=source.replace(/refresh\(\);\s*$/, '');
vm.runInContext(source,context);
vm.runInContext('let renders=0; renderOverview=()=>{renders++;};loadGroups=async()=>{await api("/api/groups");};loadMessages=async()=>{await api("/api/messages");};',context);
const flush=async()=>{for(let i=0;i<40;i++)await Promise.resolve();};
function poll(){const entry=[...timers].find(([,t])=>t.delay!==8000);assert.ok(entry,'poll scheduled');timers.delete(entry[0]);entry[1].fn();return entry[1].delay;}
(async()=>{
await vm.runInContext('refresh()',context);assert.equal(calls.length,4);assert.equal(vm.runInContext('renders',context),1);
assert.equal(poll(),1000);await flush();assert.equal(calls.length,5,'unchanged only checks signal');
token='insert-or-edit';poll();await flush();assert.equal(calls.length,9);assert.equal(vm.runInContext('renders',context),2);
document.hidden=true;listeners.visibilitychange();assert.equal(timers.size,0,'hidden tab stops polling');
document.hidden=false;listeners.visibilitychange();await flush();assert.equal(vm.runInContext('renders',context),3,'resume refreshes immediately');
failure=true;poll();await flush();assert.equal(poll(),2000);await flush();assert.equal(poll(),4000);await flush();
failure=false;listeners.online();await flush();assert.equal(vm.runInContext('retryDelay',context),1000);assert.equal(document.getElementById('alert').hidden,true);
let release;hold=new Promise(resolve=>{release=resolve;});const inFlight=vm.runInContext('refresh()',context);await flush();
vm.runInContext('refresh();refresh(false);',context);await flush();assert.equal(active,1,'only one request while busy');
release();hold=null;await inFlight;await flush();assert.equal(maxActive,1,'serial chain never overlaps requests');
assert.equal([...timers.values()].filter(t=>t.delay!==8000).length,1,'one pending poll');
console.log('dashboard poll harness: passed (auto update, unchanged, serial, hidden/resume, error/reconnect)');
})().catch(error=>{console.error(error);process.exitCode=1;});
