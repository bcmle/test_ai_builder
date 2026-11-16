import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Presentation, PresentationFile } from '@oai/artifact-tool';

const root = path.dirname(fileURLToPath(import.meta.url));
const out = path.resolve(process.argv[2] || path.join(root, '.build'));
await fs.mkdir(out, { recursive: true });
const data = JSON.parse(await fs.readFile(path.join(root, 'data.json'), 'utf8'));
const p = Presentation.create({ slideSize: { width: 1280, height: 720 } });
const C = { ink:'#282136', paper:'#F5F4E9', muted:'#66616F', green:'#96E3B1', amber:'#E3DFED', line:'#D6D1DF', white:'#282136', lime:'#96E3B1', dark:'#282136', lightMuted:'#66616F' };
const font = 'Arial';
const site = 'https://ai-builder-lab.com/';
const compare = `${site}compare?models=23c49394cadf4e0ab3b7f78d9d70373a,03b5d22868b44bbb975d3cfc01d46f93&run=${data.run_id}&revision=${data.revision_id}`;
function text(s,t,x,y,w,h,size=26,color=C.ink,bold=false,link=null){
 const q=s.shapes.add({geometry:'textbox',name:t.slice(0,65),position:{left:x,top:y,width:w,height:h},fill:'none',line:{fill:'none',width:0}});
 q.text=t;q.text.style={fontFamily:font,fontSize:size,color,bold,insets:0,verticalAlignment:'top',wrap:true};
 if(link)q.hyperlink={uri:link,isExternal:true};return q;
}
function slide(i,bg=C.paper){
 const s=p.slides.add();s.background.fill=bg;
 const sources=[data.source_url,'https://github.com/bcmle/test_ai_builder',site+'methodology'];
 s.speakerNotes.text=`Источники: ${sources.join('\n')}\nСрез: ${data.published_at}; revision ${data.revision_id}.`;
 return s;
}
function folio(s,i,dark=false,label='AI BUILDER'){
 text(s,label,64,673,1050,25,15,dark?C.lightMuted:C.muted,false,label.startsWith('ai-builder-lab.com')?(i===4?compare:site):null);
 text(s,`${i+1}/7`,1162,670,62,29,19,dark?C.lime:C.ink,true);
}
function kicker(s,t){const q=text(s,t,64,40,1120,30,16,C.ink);q.text.style={fontFamily:'Courier New',fontSize:16,color:C.ink,insets:0,verticalAlignment:'top'};}
function rect(s,x,y,w,h,fill){return s.shapes.add({geometry:'rect',position:{left:x,top:y,width:w,height:h},fill,line:{fill:'none',width:0}});}
function mono(s,t,x,y,w,h,size=16){const q=text(s,t,x,y,w,h,size,C.ink);q.text.style={fontFamily:'Courier New',fontSize:16,color:C.ink,insets:0,verticalAlignment:'top'};return q;}
async function image(s,name,x,y,w,h,fit='contain'){
 const b=await fs.readFile(path.join(root,'assets',name));
 s.images.add({blob:b.buffer.slice(b.byteOffset,b.byteOffset+b.byteLength),contentType:'image/png',alt:name==='optical-filter.png'?'Концептуальный образ оптического фильтра':name==='home.png'?'Опубликованный сайт AI Builder':'Опубликованная страница сравнения карточек',fit,position:{left:x,top:y,width:w,height:h}});
}
function table(s,vals,x,y,width,height,widths){
 const t=s.tables.add({rows:vals.length,columns:vals[0].length,left:x,top:y,width,height,columnWidths:widths,values:vals});
 t.borders.assign({fill:C.paper,width:0});
 for(let r=0;r<vals.length;r++){
  t.rows[r].height=height/vals.length;
  for(let c=0;c<vals[0].length;c++){
   const q=t.getCell(r,c);q.fill=r===0?C.amber:r===1?C.green:C.paper;
   q.text.style={fontFamily:font,fontSize:r===1&&c>0?29:r===0?20:23,color:r===0?C.white:C.ink,bold:r===0||r===1};
  }
 }
 return t;
}

let s=slide(0);
kicker(s,'AI BUILDER / ЗАЩИТА ПРОЕКТА');
// Typographic framing follows the flat bracket composition of The Pitch.
text(s,'[',925,96,125,218,190,C.amber,true);
text(s,']',1100,96,125,218,190,C.amber,true);
rect(s,990,181,146,28,C.green);
text(s,'Проверка\nбизнес-моделей',64,143,1000,190,76,C.ink,true);
rect(s,65,333,610,9,C.green);
text(s,'Сайт для выбора идеи и платного теста за 90 дней',67,370,1000,70,30,C.muted);
for(const [x,n,l,fill] of [[64,'20','МОДЕЛЕЙ',C.amber],[453,'5','УСЛОВНЫХ ОЦЕНОК',C.green],[842,'15','ТРЕБУЮТ ПРОВЕРКИ',C.amber]]){
 rect(s,x,476,374,162,fill);mono(s,l,x+22,497,335,30,15);text(s,n,x+20,534,325,92,77,C.ink,true);
}
folio(s,0,false,'ai-builder-lab.com');

s=slide(1);
kicker(s,'РАБОТАЮЩИЙ ПРОДУКТ');
text(s,'Сайт и сервер с базой данных',64,93,1150,76,52,C.ink,true);
rect(s,64,187,413,56,C.green);
text(s,'ai-builder-lab.com',82,197,390,42,30,C.ink,true,site);
rect(s,528,185,688,393,C.amber);
await image(s,'home.png',540,197,664,369);
text(s,'Aeza',64,284,400,44,30,C.ink,true);
text(s,'AI-агенты работают на сервере',64,333,407,68,25,C.muted);
text(s,'PostgreSQL',64,424,400,44,30,C.ink,true);
text(s,'Состояние, результаты, расходы\nи продолжение после остановки',64,475,430,90,23,C.muted);
rect(s,64,599,1152,48,C.green);
mono(s,'Источники / Отбор / Критик / Проверка / Оценка / Карточка',82,613,1110,30,20);
text(s,'HTTPS · Исследование запускает оператор',64,563,452,29,17,C.muted);
folio(s,1,false,'Комплект: сайт • GitHub с историей • 5 решений • ограничения • видео до 3 минут');

s=slide(2);
kicker(s,'ОБЯЗАТЕЛЬНЫЕ ФАКТОРЫ');
text(s,'Где не хватает доказательств',64,87,1160,72,52,C.ink,true);
text(s,'20 моделей в каталоге. Явных отказов в текущем срезе: 0.',64,162,1130,43,25,C.muted);
const criteria=[...data.criteria].reverse();
s.charts.add('bar',{
 position:{left:60,top:213,width:1156,height:388},categories:criteria.map(v=>`${v.id}  ${v.label}`),
 series:[{name:'Подтверждено',values:criteria.map(v=>v.passed),fill:C.green,dataLabels:{showValue:true,position:'center',numberFormatCode:'0;-0;;',textStyle:{typeface:font,fontSize:20,bold:true,fill:C.dark}}},{name:'Не подтверждено',values:criteria.map(v=>v.unknown),fill:C.amber}],
 barOptions:{direction:'bar',grouping:'stacked',gapWidth:76,overlap:100},hasLegend:true,
 legend:{position:'bottom',textStyle:{typeface:font,fontSize:20,fill:C.ink}},
 xAxis:{visible:true,textStyle:{typeface:font,fontSize:20,fill:C.ink},line:{fill:'none',width:0}},
 yAxis:{visible:false,min:0,max:20,majorGridlines:null,line:{fill:'none',width:0}},
 dataLabels:{showValue:true,position:'center',numberFormatCode:'0;-0;;',textStyle:{typeface:font,fontSize:20,bold:true,fill:C.ink}},chartFill:C.paper,plotAreaFill:C.paper
});
text(s,'F01 и F03 остаются открытыми у всех 20 моделей',64,618,1150,38,28,C.ink,true);
folio(s,2,false,'16.11.2026. Включены предыдущие оценки. Одна модель может иметь несколько пробелов.');

s=slide(3);
kicker(s,'ПРОВЕРКА И НЕШТАТНЫЕ СИТУАЦИИ');
text(s,'Две роли и явные правила ошибок',64,91,1152,77,51,C.ink,true);
rect(s,64,192,565,180,C.amber);rect(s,645,192,571,180,C.green);
mono(s,'01 / КРИТИК',87,211,510,30,18);
text(s,'Что помешает запуску\nи первой оплате?',87,261,510,94,32,C.ink,true);
mono(s,'02 / ПРОВЕРЯЮЩИЙ',668,211,520,30,18);
text(s,'Тезисы и документы\nбез выводов критика',668,261,520,94,32,C.ink,true);
text(s,'5 моделей прошли оба этапа на реальных данных',64,392,1152,39,28,C.ink,true);
rect(s,64,448,1152,1,C.line);
mono(s,'СБОЙ LLM-ПРОВАЙДЕРА',64,475,550,30,17);
text(s,'Ограниченные повторы, сохранённый\nпрогресс. Сбой ≠ отказ модели.',64,518,550,80,25,C.ink);
mono(s,'ПРОТИВОРЕЧИЕ В ИСТОЧНИКАХ',668,475,548,30,17);
text(s,'Обе версии видны. Существенный\nнерешённый конфликт блокирует рейтинг.',668,518,548,90,25,C.ink);
text(s,'Паспорт утверждения: источник, дата, метод, уверенность',64,626,1152,33,22,C.muted);
folio(s,3);

s=slide(4);
kicker(s,'ИНСТРУМЕНТ ДЛЯ СОБСТВЕННИКА');
text(s,'Карточка — главный результат',64,88,1152,78,52,C.ink,true);
text(s,'3 рядом → за 2 минуты\nвыбрать, что запускать',64,201,565,86,29,C.ink,true);
text(s,'Единая структура: клиент, выручка,\nсроки, бюджет, риски, источники',64,313,565,68,24,C.muted);
await image(s,'compare-results.png',664,174,552,235);
table(s,[['Параметр','Электронное подписание\nАналог: Docusign','Туристические рефералы\nАналог: KAYAK'],['Оценка / 100','56,25–66,25','62,5–72,5'],['До первой оплаты','43–91 день','45–110 дней'],['Бюджет теста','$4 000–12 000','$4 600–9 700']],64,426,1152,192,[330,411,411]);
rect(s,64,628,1152,38,C.amber);
text(s,'Оценки условные. У обеих моделей открыты F01 и F03.',76,634,1120,30,22,C.ink);
folio(s,4,false,'ai-builder-lab.com/compare • До 3 моделей одной версии исследования');

s.speakerNotes.text+=`\nСтраница сравнения: ${compare}\nКомпании — аналоги предлагаемых моделей. Сроки и бюджеты — аналитические оценки. В рисках туристических рефералов отмечено возможное ожидание партнёрского одобрения.\nСсылка для демонстрации: ${compare}`;

s=slide(5);
rect(s,64,209,370,436,C.green);
kicker(s,'ЛОГИКА ОЦЕНКИ');
text(s,'Оценка под конкретный запуск',64,88,1150,75,52,C.ink,true);
text(s,'90',82,223,330,174,145,C.ink,true);
text(s,'дней до первой оплаты',87,405,320,85,30,C.ink,true);
text(s,'2 человека\n$15 000 без зарплат\nРынок США',87,517,325,112,25,C.ink);
const factors=[...data.factors].reverse();
s.charts.add('bar',{
 position:{left:470,top:217,width:745,height:369},categories:factors.map(v=>v.label),series:[{name:'Вес',values:factors.map(v=>v.weight),fill:C.green}],
 barOptions:{direction:'bar',grouping:'clustered',gapWidth:125},hasLegend:false,
 xAxis:{visible:true,textStyle:{typeface:font,fontSize:22,fill:C.ink},line:{fill:'none',width:0}},
 yAxis:{visible:false,min:0,max:31,majorGridlines:null,line:{fill:'none',width:0}},
 dataLabels:{showValue:true,position:'outEnd',numberFormatCode:'0"%"',textStyle:{typeface:font,fontSize:26,bold:true,fill:C.ink}},chartFill:C.paper,plotAreaFill:C.paper
});
text(s,'50% — клиент и спрос: без них не будет оплаты.\nВеса в конфигурации; диапазоны отражают неопределённость.',470,598,746,65,21,C.ink);
folio(s,5,false,'Балл не является статистической вероятностью успеха.');

s=slide(6);
kicker(s,'ОГРАНИЧЕНИЯ И РАЗВИТИЕ');
text(s,'Что осталось проверить',64,88,1160,78,56,C.ink,true);
rect(s,64,181,1152,113,C.amber);
mono(s,'ЧЕСТНЫЕ ОГРАНИЧЕНИЯ',87,197,1090,27,17);
text(s,'15 моделей требуют проверки · F01/F03 остаются открытыми\nНет кнопки запуска на сайте · Платного пилота ещё нет',87,234,1090,60,25,C.ink);
rect(s,64,314,568,144,C.green);rect(s,648,314,568,144,C.amber);
mono(s,'СЛЕДУЮЩИЙ ШАГ',87,332,518,28,17);
text(s,'Закрыть условия одной модели\nи провести платный пилот',87,377,518,75,28,C.ink,true);
mono(s,'ПОЛНАЯ ВЕРСИЯ',671,332,518,28,17);
text(s,'Кабинет владельца\nи история экспериментов',671,377,518,75,28,C.ink,true);
mono(s,'ПЕРЕСМОТРЕЛ БЫ В ТЗ',64,486,365,31,17);
text(s,'Точную вероятность без статистики\nи обязательность аналога от $300 млн',447,480,765,65,23,C.ink);
rect(s,64,562,1152,1,C.line);
const repo='https://github.com/bcmle/test_ai_builder';
const deckRoot=repo+'/blob/codex/ceo-presentation/presentation/';
const links=[
 ['Сайт: ai-builder-lab.com',site],['Сравнение карточек',compare],
 ['GitHub · исходный код',repo],['5 решений и ограничения',deckRoot+'decisions.md'],
 ['Презентация · PPTX',deckRoot+'AI-Builder-CEO.pptx'],
 ['Методика оценки',site+'methodology']
];
for(let i=0;i<links.length;i++){
 const x=64+(i%3)*390,y=587+Math.floor(i/3)*42;
 text(s,links[i][0]+' ↗',x,y,370,32,18,C.ink,true,links[i][1]);
}
folio(s,6,false,'Все ссылки кликабельны');
s.speakerNotes.text+='\n\nСсылки:\n'+links.map(([label,url])=>label+': '+url).join('\n');

await (await PresentationFile.exportPptx(p)).save(path.join(out,'candidate.pptx'));
for(let i=0;i<p.slides.items.length;i++){
 const b=await p.export({slide:p.slides.items[i],format:'png',scale:1});
 await fs.writeFile(path.join(out,`slide-${i+1}.png`),new Uint8Array(await b.arrayBuffer()));
}
console.log(JSON.stringify({slides:p.slides.items.length,out}));
