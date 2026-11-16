import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Presentation, PresentationFile } from '@oai/artifact-tool';

const root = path.dirname(fileURLToPath(import.meta.url));
const out = path.resolve(process.argv[2] || path.join(root, '.build'));
await fs.mkdir(out, { recursive: true });
const data = JSON.parse(await fs.readFile(path.join(root, 'data.json'), 'utf8'));
const p = Presentation.create({ slideSize: { width: 1280, height: 720 } });
const C = { ink:'#161B19', paper:'#F8F9F6', muted:'#59635E', green:'#207A60', amber:'#E8BF64', line:'#DCE2DA', white:'#FFFFFF' };
const font = 'Arial';
const site = 'https://ai-builder-lab.com/';
const compare = `${site}compare?models=23c49394cadf4e0ab3b7f78d9d70373a,03b5d22868b44bbb975d3cfc01d46f93&run=${data.run_id}&revision=${data.revision_id}`;
const notes = [
  'Я создал и развернул сайт для исследования бизнес-моделей. Он помогает выбрать, какую идею проверить платным тестом за девяносто дней. В каталоге двадцать моделей. Пять получили условные оценки после проверки агентами, пятнадцать требуют дополнительного исследования.',
  'Исследование работает на сервере Aeza: система собирает источники, проверяет обязательные критерии, запускает критика и проверяющего, затем формирует карточку. PostgreSQL хранит состояние, результаты и расходы. Прерванную работу можно продолжить. Сейчас запуск выполняет оператор на сервере, а сайт позволяет просматривать результаты.',
  'Здесь показаны все двадцать моделей по десяти обязательным факторам. Зелёным отмечено подтверждение, жёлтым — нехватка оснований. Явных отказов в текущих карточках нет. Главные пробелы — законность конкретного запуска и необходимость собственной лицензии. Это открытые вопросы, поэтому система сохраняет их рядом с оценкой, а не скрывает. Одна модель может иметь несколько пробелов.',
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
function slide(i,dark=false){
 const s=p.slides.add();s.background.fill=dark?C.ink:C.paper;
 if(i>0)text(s,titles[i],60,44,1150,68,43,dark?C.white:C.ink,true);
 text(s,`${String(i+1).padStart(2,'0')} / 07`,1155,674,85,25,16,dark?'#C8D6CF':C.muted);
 const sources=[data.source_url,'https://github.com/bcmle/test_ai_builder',site+'methodology'];
 s.speakerNotes.text=`${timings[i]}\n\n${notes[i]}\n\nИсточники: ${sources.join('\n')}\nСрез: ${data.published_at}; revision ${data.revision_id}.`;
 return s;
}
async function image(s,name,x,y,w,h){const b=await fs.readFile(path.join(root,'assets',name));s.images.add({blob:b.buffer.slice(b.byteOffset,b.byteOffset+b.byteLength),contentType:'image/png',alt:name==='home.png'?'Главный экран опубликованного сайта':'Реальная страница сравнения Docusign и KAYAK',fit:'contain',position:{left:x,top:y,width:w,height:h}});}
function table(s,vals,x,y,width,height,widths){
 const t=s.tables.add({rows:vals.length,columns:vals[0].length,left:x,top:y,width,height,columnWidths:widths,values:vals});
 t.borders.assign({fill:C.line,width:1});
 for(let r=0;r<vals.length;r++){
  t.rows[r].height=height/vals.length;
  for(let c=0;c<vals[0].length;c++){
   const q=t.getCell(r,c);q.fill=r===0?C.ink:C.paper;
   q.text.style={fontFamily:font,fontSize:23,color:r===0?C.white:C.ink,bold:r===0};
  }
 }
 return t;
}

let s=slide(0);
text(s,'AI BUILDER',60,50,480,35,22,C.green,true);
text(s,titles[0],60,138,610,160,57,C.ink,true);
text(s,'Выбор идеи для платного теста\nза 90 дней',60,327,580,88,30,C.muted);
await image(s,'home.png',680,135,540,304);
for(const [x,n,l] of [[60,'20','моделей в каталоге'],[465,'5','условных оценок'],[870,'15','требуют проверки']]){
 text(s,n,x,486,310,95,72,C.green,true);text(s,l,x,584,345,45,24,C.muted);
}
text(s,'ai-builder-lab.com',60,674,550,26,18,C.muted,false,site);

s=slide(1);
text(s,'Полный путь исследования работает на сервере',60,125,1120,42,27,C.muted);
const stages=[['01','Источники'],['02','Отбор'],['03','Критик'],['04','Проверка'],['05','Оценка'],['06','Карточка']];
stages.forEach(([n,label],i)=>{const x=60+i*195;text(s,n,x,217,150,68,47,C.green,true);text(s,label,x,294,185,45,26,C.ink,true);});
text(s,'Сайт',60,413,320,45,30,C.ink,true);
text(s,'Домен и HTTPS\nКарточки и сравнение',60,473,330,100,25,C.muted);
text(s,'Сервер Aeza',460,413,330,45,30,C.ink,true);
text(s,'Исследования и AI-агенты\nЗапуск оператором',460,473,365,100,25,C.muted);
text(s,'PostgreSQL',860,413,350,45,30,C.ink,true);
text(s,'Состояние и результаты\nУчёт расходов и возобновление',860,473,365,110,25,C.muted);
text(s,'Публичный сайт показывает результаты. Платные исследования запускаются отдельно на сервере.',60,625,1125,45,20,C.muted);

s=slide(2);
text(s,'20 моделей: подтверждения и открытые вопросы по каждому фактору',60,119,1160,40,25,C.muted);
const chart=s.charts.add('bar',{
 position:{left:58,top:177,width:1162,height:427},categories:[...data.criteria].reverse().map(v=>`${v.id}  ${v.label}`),
 series:[{name:'Подтверждено',values:[...data.criteria].reverse().map(v=>v.passed),fill:C.green},{name:'Не подтверждено',values:[...data.criteria].reverse().map(v=>v.unknown),fill:C.amber}],
 barOptions:{direction:'bar',grouping:'stacked',gapWidth:54,overlap:100},hasLegend:true,
 legend:{position:'bottom',textStyle:{typeface:font,fontSize:20,fill:C.ink}},
 xAxis:{visible:true,textStyle:{typeface:font,fontSize:21,fill:C.ink},line:{fill:'none',width:0}},
 yAxis:{visible:false,min:0,max:20,majorGridlines:null,line:{fill:'none',width:0}},
 dataLabels:{showValue:true,position:'center',numberFormatCode:'0;-0;;',textStyle:{typeface:font,fontSize:21,bold:true,fill:C.ink}},chartFill:C.paper,plotAreaFill:C.paper
});
text(s,'Явных отказов: 0. Неподтверждённый фактор остаётся открытым вопросом.',60,616,1155,32,23,C.ink,true);
text(s,'Срез 16.11.2026: текущие карточки каталога, включая предыдущие оценки. Одна модель может иметь несколько пробелов.',60,655,1090,39,16,C.muted);

s=slide(3);
text(s,'Пять оценённых моделей прошли оба этапа на реальных данных',60,122,1160,43,26,C.green,true);
text(s,'Критик',60,209,480,53,36,C.ink,true);
text(s,'Ищет причины провала\nзапуска и первой оплаты',60,283,495,98,29,C.muted);
text(s,'Проверяющий',680,209,530,53,36,C.ink,true);
text(s,'Проверяет нейтральные тезисы\nпо документам без оценок\nи бизнес-выводов критика',680,283,530,142,29,C.muted);
text(s,'Пример: туристические рефералы',60,450,1100,44,27,C.green,true);
text(s,'Партнёрское одобрение может задержать первую оплату',60,506,1140,47,31,C.ink,true);
text(s,'Паспорт утверждения: источник, дата, метод, уверенность',60,593,1130,49,26,C.muted);

s=slide(4);
text(s,'До трёх моделей одной версии исследования рядом',60,122,1130,44,27,C.muted);
await image(s,'compare-results.png',60,208,440,248);
text(s,'Реальный экран сайта',60,471,440,34,20,C.muted);
table(s,[['Параметр','Подписание\nаналог Docusign','Рефералы\nаналог KAYAK'],['Оценка / 100','56,25–66,25','62,5–72,5'],['До оплаты','43–91 день','45–110 дней'],['Бюджет теста','$4 000–12 000','$4 600–9 700']],550,200,670,330,[206,232,232]);
text(s,'Оценки условные. У обеих моделей открыты F01 и F03.',60,558,1140,39,25,C.green,true);
text(s,'Сравниваем сроки, бюджет, риски и условия запуска',60,610,1130,42,29,C.ink,true);
text(s,'Открыть сравнение на сайте',60,671,650,26,18,C.muted,false,compare);
s.speakerNotes.text+=`\nСтраница сравнения: ${compare}\nСроки и бюджет — аналитические диапазоны, а не результаты фактических запусков. Компании показаны как аналоги.`;

s=slide(5);
const weights=s.charts.add('bar',{
 position:{left:60,top:168,width:770,height:414},categories:[...data.factors].reverse().map(v=>v.label),series:[{name:'Вес',values:[...data.factors].reverse().map(v=>v.weight),fill:C.green}],
 barOptions:{direction:'bar',grouping:'clustered',gapWidth:85},hasLegend:false,
 xAxis:{visible:true,textStyle:{typeface:font,fontSize:24,fill:C.ink},line:{fill:'none',width:0}},
 yAxis:{visible:false,min:0,max:30,majorGridlines:null,line:{fill:'none',width:0}},
 dataLabels:{showValue:true,position:'outEnd',numberFormatCode:'0"%"',textStyle:{typeface:font,fontSize:25,bold:true,fill:C.green}},chartFill:C.paper,plotAreaFill:C.paper
});
text(s,'90 дней',910,185,320,84,55,C.green,true);
text(s,'2 человека\n$15 000 без зарплат\nРынок США',910,298,315,158,26,C.ink);
text(s,'Единый сценарий\nдля сравнения',910,497,315,82,23,C.muted);
text(s,'Веса находятся в конфигурации. Диапазон баллов отражает неопределённость.',60,609,1140,43,25,C.ink);
text(s,'Оценка не является статистической вероятностью успеха.',60,659,1080,30,18,C.muted);

s=slide(6,true);
text(s,'Проверка на практике',60,194,510,52,32,'#A7DFC7',true);
text(s,'Выбрать одну модель\nЗакрыть условия запуска\nПроверить спрос платным пилотом',60,280,550,190,29,C.white);
text(s,'Полная версия',720,194,490,52,32,'#A7DFC7',true);
text(s,'Кабинет владельца\nУправление исследованиями\nИстория экспериментов',720,280,500,190,29,C.white);
text(s,'Что пересмотрел бы в задании',60,496,1120,41,27,'#A7DFC7',true);
text(s,'Точную вероятность без статистики запусков.\nОбязательность аналога стоимостью от $300 млн.',60,551,1130,86,26,C.white);
text(s,'ai-builder-lab.com',60,674,430,26,18,'#C8D6CF',false,site);
text(s,'github.com/bcmle/test_ai_builder',550,674,600,26,18,'#C8D6CF',false,'https://github.com/bcmle/test_ai_builder');

await (await PresentationFile.exportPptx(p)).save(path.join(out,'candidate.pptx'));
for(let i=0;i<p.slides.items.length;i++){
 const b=await p.export({slide:p.slides.items[i],format:'png',scale:1});
 await fs.writeFile(path.join(out,`slide-${i+1}.png`),new Uint8Array(await b.arrayBuffer()));
}
await fs.writeFile(path.join(out,'speaker-notes.md'),'# Сценарий защиты: 2 минуты 50 секунд\n\n'+notes.map((v,i)=>`## ${i+1}. ${titles[i].replaceAll('\n',' ')} (${timings[i]})\n\n${v}`).join('\n\n')+'\n');
console.log(JSON.stringify({slides:p.slides.items.length,out,spokenWords:notes.join(' ').split(/\s+/).length}));
