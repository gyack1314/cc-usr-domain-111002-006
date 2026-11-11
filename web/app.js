const $=(s,root=document)=>root.querySelector(s);
const $$=(s,root=document)=>[...root.querySelectorAll(s)];
const data=await fetch('/data.json').then(r=>r.json());
const runtime=()=>fetch('/api/runtime').then(r=>r.json());
const action=(name,fields={})=>fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,...fields})}).then(r=>r.json());
const tick=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
const html=(value)=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
const show=(s)=>{$('#status').textContent=s;};

/*
 * 展开状态只保存在页面内：折叠节点的子节点不在 DOM 中。
 * expandedKeys 记录成功展开的节点路径键，failedKeys 记录加载失败的节点。
 * rebuild-tree 阶段每次展开都会整体重建 #tree，旧元素句柄随之失效，
 * 但节点的 node-id 与路径键保持稳定，接入端必须按 id+路径重新定位。
 */
const expandedKeys=new Set();
const failedKeys=new Set();
/* rebuilds 统计“展开导致的子树重建”次数；初始渲染不计入，便于接入端等待本次点击后的新 DOM。 */
let rebuilds=0;
window.__portalRebuilds=()=>rebuilds;

function targetOf(node){return node.kind==='shortcut'?data.nodes[node.target]:node;}

function nodeHTML(node,path){
  const key=path+'/'+node.id;
  const targetAttr=node.kind==='shortcut'?` data-target="${html(node.target)}"`:'';
  const loaded=failedKeys.has(key)?'error':(expandedKeys.has(key)?'true':'false');
  const expandedAttr=expandedKeys.has(key)?' aria-expanded="true"':'';
  let head;
  if(node.kind==='file'){
    head=`<span>${html(node.name)}</span><span data-file-id="${html(node.file)}">${html(node.file)}</span>`;
  }else if(node.kind==='denied'){
    head=`<span>${html(node.name)}</span><span>不可读取</span>`;
  }else{
    head=`<span>${html(node.name)}</span><button data-expand="${html(key)}">展开</button>`;
  }
  let group='';
  if(expandedKeys.has(key)){
    const target=targetOf(node);
    group=(target.children||[]).map(id=>nodeHTML(data.nodes[id],key)).join('');
  }
  return `<div role="treeitem" data-node-id="${html(node.id)}" data-path="${html(key)}" data-kind="${html(node.kind)}"${targetAttr} data-loaded="${loaded}"${expandedAttr}><div class="row">${head}</div><div role="group">${group}</div></div>`;
}

function render(){
  rebuilds+=1;
  $('#tree').setAttribute('data-rebuild',String(rebuilds));
  $('#tree').innerHTML=nodeHTML(data.nodes.root,'');
}
render();
show('目录入口已加载');

$('#tree').onclick=async event=>{
  const button=event.target.closest('[data-expand]');
  if(!button)return;
  const item=button.closest('[role=treeitem]');
  const node=data.nodes[item.dataset.nodeId];
  const key=item.dataset.path;
  const target=targetOf(node);
  await action('expand',{node:node.id,path:key});
  const state=await runtime();
  if(state.phase==='moved-tree'&&node.id==='north')target.children=[];
  if(state.phase==='failed-tree'&&node.id==='south'){
    failedKeys.add(key);
    expandedKeys.delete(key);
    render();
    show('南区目录加载失败');
    return;
  }
  await tick();
  failedKeys.delete(key);
  expandedKeys.add(key);
  render();
  show('目录已展开');
};
