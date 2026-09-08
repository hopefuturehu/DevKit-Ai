// A deliberately small safe Markdown renderer: HTML is always text. No innerHTML.
export function markdown(text) {
  const fragment=document.createDocumentFragment();
  const lines=String(text||'').split('\n');
  let code=null;
  const inline=(parent,value)=>{
    const pattern=/(`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\((?:https?:\/\/|mailto:)[^\s)]+\))/g;
    let last=0;
    for(const match of value.matchAll(pattern)){
      parent.append(document.createTextNode(value.slice(last,match.index)));
      const part=match[0]; let node;
      if(part.startsWith('`')){node=document.createElement('code');node.textContent=part.slice(1,-1);}
      else if(part.startsWith('**')){node=document.createElement('strong');node.textContent=part.slice(2,-2);}
      else {const link=part.match(/^\[([^\]]+)\]\((.+)\)$/);node=document.createElement('a');node.textContent=link[1];node.href=link[2];node.target='_blank';node.rel='noopener noreferrer';}
      parent.append(node);last=match.index+part.length;
    }
    parent.append(document.createTextNode(value.slice(last)));
  };
  let list=null;
  for(const line of lines){
    if(line.startsWith('```')){if(code){code=null;}else{const pre=document.createElement('pre');code=document.createElement('code');pre.append(code);fragment.append(pre);}list=null;continue;}
    if(code){code.textContent+=line+'\n';continue;}
    if(!line.trim()){list=null;continue;}
    const item=line.match(/^\s*(?:[-*]|\d+\.)\s+(.*)/);
    if(item){if(!list){list=document.createElement('ul');fragment.append(list);}const li=document.createElement('li');inline(li,item[1]);list.append(li);continue;}
    list=null;
    const heading=line.match(/^(#{1,4})\s+(.*)/);
    const p=document.createElement(heading?'h'+Math.min(heading[1].length+1,5):'p');
    inline(p,heading?heading[2]:line);fragment.append(p);
  }
  return fragment;
}
