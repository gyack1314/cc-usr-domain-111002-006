const $=(s,root=document)=>root.querySelector(s);
const $$=(s,root=document)=>[...root.querySelectorAll(s)];
const data=await fetch('/data.json').then(r=>r.json());
const runtime=()=>fetch('/api/runtime').then(r=>r.json());
const action=(name,fields={})=>fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,...fields})}).then(r=>r.json());
const tick=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
const html=(value)=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
const show=(s)=>{$('#status').textContent=s;};

function nodeHTML(node,path){const key=path+'/'+node.id;return `<div role="treeitem" data-node-id="${node.id}" data-path="${key}" data-kind="${node.kind}" data-loaded="false"><span>${node.name}</span>${node.kind==='file'?`<span data-file-id="${node.file}">${node.file}</span>`:node.kind==='denied'?'<span>不可读取</span>':`<button data-expand="${key}">展开</button>`}<div role="group"></div></div>`;}
$('#tree').innerHTML=nodeHTML(data.nodes.root,'');
$('#tree').onclick=async event=>{const button=event.target.closest('[data-expand]');if(!button)return;const item=button.closest('[role=treeitem]');const node=data.nodes[item.dataset.nodeId];const target=node.kind==='shortcut'?data.nodes[node.target]:node;await action('expand',{node:node.id,path:item.dataset.path});const state=await runtime();if(state.phase==='moved-tree'&&node.id==='north')target.children=[];if(state.phase==='failed-tree'&&node.id==='south'){item.dataset.loaded='error';show('南区目录加载失败');return;}await tick();item.querySelector('[role=group]').innerHTML=(target.children||[]).map(id=>nodeHTML(data.nodes[id],item.dataset.path)).join('');item.dataset.loaded='true';item.setAttribute('aria-expanded','true');show('目录已展开');};show('目录入口已加载');
