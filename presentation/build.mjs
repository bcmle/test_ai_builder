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
const notes = [
  'Я создал и развернул сайт для исследования бизнес-моделей. Он помогает выбрать, какую идею проверить платным тестом за девяносто дней. В каталоге двадцать моделей. Пять получили условные оценки после проверки агентами, пятнадцать требуют дополнительного исследования.',
  'Исследование работает на сервере Aeza: система собирает источники, проверяет обязательные критерии, запускает критика и проверяющего, затем формирует карточку. PostgreSQL хранит состояние, результаты и расходы. Прерванную работу можно продолжить. Сейчас запуск выполняет оператор на сервере, а сайт позволяет просматривать результаты.',
  'Здесь показаны все двадцать моделей по десяти обязательным факторам. Мятным отмечено подтверждение, светло-сиреневым — нехватка оснований. Явных отказов в текущих карточках нет. Главные пробелы — законность конкретного запуска и необходимость собственной лицензии. Это открытые вопросы, поэтому система сохраняет их рядом с оценкой, а не скрывает. Одна модель может иметь несколько пробелов.',
  'Критик ищет причины, почему запуск может не состояться. Проверяющий получает нейтральные тезисы и документы без оценок и бизнес-выводов критика. Утверждения связаны с источниками и паспортами. Оба этапа реально выполнены для пяти оценённых моделей. Например, для туристических рефералов остаётся риск, что партнёрское одобрение не удастся получить вовремя.',
  'На сайте можно выбрать до трёх карточек одной версии и сравнить их рядом. Здесь электронное подписание и туристические рефералы. Видны покупатель, выручка, оценка, бюджет и время до оплаты. У рефералов оценка выше, но срок может выйти за девяносто дней. Поэтому решение требует сравнения условий, а не только числа. Из таблицы можно перейти к рискам и источникам. Условная оценка означает завершённый анализ с открытыми условиями запуска.',
  'Половину оценки определяют доступ к покупателям и свидетельства готовности платить. Остальное — подготовка предложения, срок до оплаты и ресурсы. Веса находятся в конфигурации. Все варианты оцениваются для команды из двух человек с бюджетом пятнадцать тысяч долларов без зарплат. Диапазоны отражают неопределённость. Балл не является вероятностью успеха.',
  'Дальше я бы выбрал одну модель, закрыл юридические и партнёрские условия и проверил спрос платным пилотом. В полной версии добавил бы кабинет владельца и историю экспериментов. Из задания убрал бы точную вероятность без статистики запусков, а обязательный размер аналога предложил бы пересмотреть. Сайт и исходный код доступны по ссылкам.'
];
const titles = ['Конвейер проверки\nбизнес-моделей','Сайт и серверный конвейер','Обязательные факторы F01–F10','Критик и независимый проверяющий','Сравнение карточек на сайте','Оценка готовности к первой оплате','Следующий шаг: платный пилот'];
const timings = ['0:00–0:15','0:15–0:35','0:35–1:05','1:05–1:30','1:30–2:10','2:10–2:30','2:30–2:50'];
function text(s,t,x,y,w,h,size=26,color=C.ink,bold=false,link=null){
 const q=s.shapes.add({geometry:'textbox',name:t.slice(0,65),position:{left:x,top:y,width:w,height:h},fill:'none',line:{fill:'none',width:0}});
 q.text=t;q.text.style={fontFamily:font,fontSize:size,color,bold,insets:0,verticalAlignment:'top',wrap:true};
 if(link)q.hyperlink={uri:link,isExternal:true};return q;
}
function slide(i,bg=C.paper){
 const s=p.slides.add();s.background.fill=bg;
 const sources=[data.source_url,'https://github.com/bcmle/test_ai_builder',site+'methodology'];
 s.speakerNotes.text=`${timings[i]}\n\n${notes[i]}\n\nИсточники: ${sources.join('\n')}\nСрез: ${data.published_at}; revision ${data.revision_id}.`;
 return s;
}
function folio(s,i,dark=false,label='AI BUILDER'){
 text(s,label,64,673,1050,25,15,dark?C.lightMuted:C.muted,false,label.startsWith('ai-builder-lab.com')?(i===4?compare:site):null);
 text(s,String(i+1).padStart(2,'0'),1174,670,50,29,19,dark?C.lime:C.ink,true);
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
   q.text.style={fontFamily:font,fontSize:r===1&&c>0?35:r===0?24:26,color:r===0?C.white:C.ink,bold:r===0||r===1};
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
folio(s,1,false,'HTTPS • Просмотр и сравнение на сайте • Исследование запускает оператор на сервере');

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
kicker(s,'ПРОВЕРКА ВЫВОДОВ');
text(s,'Две роли с раздельными контекстами',64,91,1152,77,51,C.ink,true);
rect(s,64,220,565,269,C.amber);rect(s,645,220,571,269,C.green);
mono(s,'01 / КРИТИК',87,243,510,30,18);
text(s,'Что помешает\nзапуску и оплате?',87,302,510,132,39,C.ink,true);
mono(s,'02 / ПРОВЕРЯЮЩИЙ',668,243,520,30,18);
text(s,'Нейтральные тезисы\nи документы',668,298,520,98,35,C.ink,true);
text(s,'Без оценок и бизнес-выводов критика',668,415,510,56,23,C.muted);
rect(s,64,507,1152,1,C.line);
text(s,'5',64,535,110,107,88,C.ink,true);
text(s,'моделей прошли оба этапа\nна реальных данных',183,554,430,79,26,C.ink);
mono(s,'ПАСПОРТ УТВЕРЖДЕНИЯ',668,548,540,30,17);
text(s,'Источник, дата, метод, уверенность',668,593,545,41,23,C.muted);
folio(s,3);

s=slide(4);
kicker(s,'ИНСТРУМЕНТ ДЛЯ СОБСТВЕННИКА');
text(s,'Карточки, которые\nможно сравнить',64,88,780,147,55,C.ink,true);
text(s,'До трёх моделей одной версии исследования',64,253,780,40,25,C.muted);
await image(s,'compare-results.png',878,80,338,190);
table(s,[['Параметр','Электронное подписание\nАналог: Docusign','Туристические рефералы\nАналог: KAYAK'],['Оценка / 100','56,25–66,25','62,5–72,5'],['До первой оплаты','43–91 день','45–110 дней'],['Бюджет теста','$4 000–12 000','$4 600–9 700']],64,326,1152,292,[330,411,411]);
rect(s,64,628,1152,38,C.amber);
text(s,'Оценки условные. У обеих моделей открыты F01 и F03.',76,634,1120,30,22,C.ink);
folio(s,4,false,'ai-builder-lab.com/compare');

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
text(s,'Веса в конфигурации. Диапазоны сохраняют неопределённость.',470,611,746,56,22,C.ink);
folio(s,5,false,'Балл не является статистической вероятностью успеха.');

s=slide(6);
kicker(s,'РАЗВИТИЕ ПРОДУКТА');
text(s,'Следующая проверка:\nреальный клиент',64,96,1160,152,61,C.ink,true);
rect(s,64,291,568,252,C.green);rect(s,648,291,568,252,C.amber);
mono(s,'01 / СЛЕДУЮЩИЙ ШАГ',87,316,518,30,17);
text(s,'Платный пилот',87,366,515,57,39,C.ink,true);
text(s,'Выбрать модель и закрыть\nусловия запуска',87,443,508,74,27,C.ink);
mono(s,'02 / ПОЛНАЯ ВЕРСИЯ',671,316,518,30,17);
text(s,'Развитие сайта',671,366,515,57,39,C.ink,true);
text(s,'Кабинет владельца\nи история экспериментов',671,443,508,74,27,C.ink);
rect(s,64,570,1152,1,C.line);
mono(s,'ПЕРЕСМОТРЕЛ БЫ В ТЗ',64,596,365,31,17);
text(s,'Точную вероятность без статистики\nи обязательность аналога от $300 млн',447,590,765,70,24,C.ink);
folio(s,6,false,'ai-builder-lab.com      /      github.com/bcmle/test_ai_builder');

await (await PresentationFile.exportPptx(p)).save(path.join(out,'candidate.pptx'));
for(let i=0;i<p.slides.items.length;i++){
 const b=await p.export({slide:p.slides.items[i],format:'png',scale:1});
 await fs.writeFile(path.join(out,`slide-${i+1}.png`),new Uint8Array(await b.arrayBuffer()));
}
await fs.writeFile(path.join(out,'speaker-notes.md'),'# Сценарий защиты: 2 минуты 50 секунд\n\n'+notes.map((v,i)=>`## ${i+1}. ${titles[i].replaceAll('\n',' ')} (${timings[i]})\n\n${v}`).join('\n\n')+'\n');
console.log(JSON.stringify({slides:p.slides.items.length,out,spokenWords:notes.join(' ').split(/\s+/).length}));
