import assert from 'node:assert/strict';
import test from 'node:test';
import {
  alphabeticLabel,ensureCompositionSettings,layoutOptions,reconcilePanelLabels,readingOrderPanels,
  planPanelLayout,applyPanelLayout,resizeFigure,panelBounds,annotationBounds,compositionBounds,textLines,
} from '../web/modules/layout-engine.js';

function fixture(count=4,{width=180,height=120,ratio=1}={}) {
  return {figure:{width_mm:width,height_mm:height,background:'#fff'},settings:{},assets:[{id:'image',width_px:1200,height_px:1200/ratio}],
    panels:Array.from({length:count},(_,index)=>({id:`p${index}`,asset_id:'image',x_mm:10+index%3*45,y_mm:10+Math.floor(index/3)*35,width_mm:40,height_mm:40/ratio,crop:{x:0,y:0,width:1200,height:1200/ratio},label:'',label_mode:'auto',label_font_size_pt:10})),annotations:[]};
}
function within(bounds,figure) {assert.ok(bounds.left>=-1e-5,`left ${bounds.left}`);assert.ok(bounds.top>=-1e-5,`top ${bounds.top}`);assert.ok(bounds.right<=figure.width_mm+1e-5,`right ${bounds.right}`);assert.ok(bounds.bottom<=figure.height_mm+1e-5,`bottom ${bounds.bottom}`);}
function disjoint(a,b) {return a.right<=b.left+1e-5||b.right<=a.left+1e-5||a.bottom<=b.top+1e-5||b.bottom<=a.top+1e-5;}

test('letters continue past Z and ZZ, with explicit numeric validation',()=>{
  assert.deepEqual([0,25,26,27,51,52,701,702].map(index=>alphabeticLabel(index)),['A','Z','AA','AB','AZ','BA','ZZ','AAA']);
  assert.equal(alphabeticLabel(26,true),'aa');
  for(const index of [-1,1.5,NaN,Infinity,'2'])assert.throws(()=>alphabeticLabel(index));
});

test('legacy labels are preserved, empty legacy panels join automatic numbering',()=>{
  const project=fixture(3);delete project.panels[0].label_mode;project.panels[0].label='Protein A';delete project.panels[1].label_mode;
  const composition=ensureCompositionSettings(project);
  assert.equal(composition.layout.gap_x_mm,3);assert.equal(composition.numbering.order,'visual');
  assert.equal(project.panels[0].label_mode,'manual');assert.equal(project.panels[1].label_mode,'auto');
  reconcilePanelLabels(project);assert.equal(project.panels[0].label,'Protein A');
});

test('deleting B then adding a panel produces unique sequential automatic labels',()=>{
  const project=fixture(3);reconcilePanelLabels(project,{order:'source'});
  project.panels.splice(1,1);reconcilePanelLabels(project);
  project.panels.push({...project.panels[1],id:'new',label:''});reconcilePanelLabels(project);
  assert.deepEqual(project.panels.map(panel=>panel.label),['A','B','C']);
});

test('manual labels are reserved case-insensitively and not overwritten during layout',()=>{
  const project=fixture(4);project.panels[1].label_mode='manual';project.panels[1].label='b';
  applyPanelLayout(project,{columns:2,rows:2});
  assert.equal(project.panels[1].label,'b');assert.deepEqual(new Set(project.panels.map(panel=>panel.label.toLowerCase())),new Set(['a','b','c','d']));
});

test('manual duplicate labels are reported, while automatic labels remain collision-free',()=>{
  const project=fixture(4);project.panels[0].label='A';project.panels[0].label_mode='manual';project.panels[1].label='a';project.panels[1].label_mode='manual';
  const report=reconcilePanelLabels(project,{order:'source'});
  assert.deepEqual(report.duplicates,[{label:'A',panel_ids:['p0','p1']}]);assert.deepEqual(project.panels.slice(2).map(panel=>panel.label),['B','C']);
});

test('manual, automatic, hidden, disabled and forced numbering have separate behavior',()=>{
  const project=fixture(3);project.panels[0].label_mode='manual';project.panels[0].label='Custom';project.panels[1].label_mode='none';project.panels[1].label='old';
  reconcilePanelLabels(project,{enabled:false});assert.deepEqual(project.panels.map(panel=>panel.label),['Custom','','']);
  reconcilePanelLabels(project,{enabled:true,style:'numeric',force:true,order:'source'});assert.deepEqual(project.panels.map(panel=>panel.label),['1','','2']);
  reconcilePanelLabels(project,{style:'lower'});assert.deepEqual(project.panels.map(panel=>panel.label),['a','','b']);
});

test('visual numbering follows final positions without changing panel array order',()=>{
  const project=fixture(4);project.panels[0].x_mm=80;project.panels[0].y_mm=80;project.panels[1].x_mm=80;project.panels[1].y_mm=10;project.panels[2].x_mm=10;project.panels[2].y_mm=10;project.panels[3].x_mm=10;project.panels[3].y_mm=80;
  const report=reconcilePanelLabels(project);
  assert.deepEqual(report.order,['p2','p1','p3','p0']);assert.deepEqual(project.panels.map(panel=>panel.id),['p0','p1','p2','p3']);assert.deepEqual(project.panels.map(panel=>panel.label),['D','B','A','C']);
});

test('reading rows tolerate different image heights and a spanning hero without merging later rows',()=>{
  const panels=[{id:'hero',x_mm:5,y_mm:20,height_mm:80},{id:'top',x_mm:90,y_mm:10,height_mm:40},{id:'bottom',x_mm:90,y_mm:65,height_mm:40}];
  assert.deepEqual(readingOrderPanels(panels).map(panel=>panel.id),['hero','top','bottom']);
});

test('label reconciliation rejects invalid options without any partial changes',()=>{
  const project=fixture();const before=structuredClone(project);
  assert.throws(()=>reconcilePanelLabels(project,{style:'roman'}));assert.deepEqual(project,before);
  project.panels[2].id=project.panels[1].id;const duplicate=structuredClone(project);assert.throws(()=>reconcilePanelLabels(project));assert.deepEqual(project,duplicate);
});

test('planning is pure, explicit 2x2 and 3x2 grids use exact requested gaps and margins',()=>{
  const four=fixture(),before=structuredClone(four),plan=planPanelLayout(four,{columns:2,rows:2,margin_mm:7,gap_x_mm:2,gap_y_mm:4});
  assert.deepEqual(four,before);assert.equal(plan.columns,2);assert.equal(plan.rows,2);
  assert.equal(plan.cells[0].x_mm,7);assert.equal(plan.cells[1].x_mm-plan.cells[0].x_mm-plan.cells[0].width_mm,2);assert.equal(plan.cells[2].y_mm-plan.cells[0].y_mm-plan.cells[0].height_mm,4);
  const six=fixture(6);const grid=applyPanelLayout(six,{columns:3,rows:2,gap_x_mm:2,gap_y_mm:2});assert.equal(grid.columns,3);assert.equal(grid.rows,2);
  six.panels.forEach(panel=>within(panelBounds(panel),six.figure));
});

test('automatic grid uses image fit and can resolve a requested single dimension',()=>{
  const project=fixture(6);let plan=planPanelLayout(project);assert.equal(plan.columns,3);assert.equal(plan.rows,2);
  plan=planPanelLayout(project,{columns:2});assert.equal(plan.columns,2);assert.equal(plan.rows,3);
  plan=planPanelLayout(project,{rows:2});assert.equal(plan.columns,3);assert.equal(plan.rows,2);
});

test('layout preserves source crop and image aspect ratios, including quarter-turn rotation',()=>{
  const project=fixture(4,{ratio:2}),crops=structuredClone(project.panels.map(panel=>panel.crop));
  project.panels[0].rotation=90;project.panels[1].rotation=37;
  applyPanelLayout(project,{columns:2,rows:2});
  assert.deepEqual(project.panels.map(panel=>panel.crop),crops);
  for(const panel of project.panels){assert.ok(Math.abs(panel.width_mm/panel.height_mm-2)<1e-5);within(panelBounds(panel),project.figure);}
  for(let i=0;i<project.panels.length;i++)for(let j=i+1;j<project.panels.length;j++)assert.ok(disjoint(panelBounds(project.panels[i]),panelBounds(project.panels[j])));
});

test('new blank automatic labels reserve physical caption space before they are assigned',()=>{
  const project=fixture(6);applyPanelLayout(project,{columns:3,rows:2,margin_mm:0,gap_y_mm:0});
  for(const panel of project.panels){assert.ok(panel.label);within(panelBounds(panel),project.figure);}
  for(let i=0;i<project.panels.length;i++)for(let j=i+1;j<project.panels.length;j++)assert.ok(disjoint(panelBounds(project.panels[i]),panelBounds(project.panels[j])));
});

test('more than 26 labels fit without cycling or clipping',()=>{
  const project=fixture(30,{width:400,height:300});applyPanelLayout(project,{columns:6,rows:5});
  assert.equal(new Set(project.panels.map(panel=>panel.label)).size,30);assert.ok(project.panels.some(panel=>panel.label==='AD'));
  project.panels.forEach(panel=>within(panelBounds(panel),project.figure));
});

test('split-channel source aspect is retained rather than the underlying single plane aspect',()=>{
  const project=fixture(1),panel=project.panels[0];panel.channel_view={mode:'split',split:{columns:2,gap_px:0,include_merge:false},channels:[{id:'c1',name:'One',visible:true},{id:'c2',name:'Two',visible:true}]};
  applyPanelLayout(project);assert.ok(Math.abs(panel.width_mm/panel.height_mm-2)<1e-5);
});

test('hero-left and hero-top create actual spanning slots and fit every panel',()=>{
  for(const preset of ['hero-left','hero-top']) {
    const project=fixture(5),plan=applyPanelLayout(project,{preset});assert.equal(plan.cells.length,5);
    assert.ok(preset==='hero-left'?plan.cells[0].row_span>1:plan.cells[0].column_span>1);
    for(const panel of project.panels)within(panelBounds(panel),project.figure);
    for(let i=0;i<project.panels.length;i++)for(let j=i+1;j<project.panels.length;j++)assert.ok(disjoint(panelBounds(project.panels[i]),panelBounds(project.panels[j])));
  }
});

test('invalid grid capacity, negative gaps, NaN and exhausted canvas fail atomically',()=>{
  for(const options of [{columns:1,rows:1},{columns:1.5},{rows:Infinity},{margin_mm:100},{gap_x_mm:-1},{gap_y_mm:NaN},{columns:4,rows:1,gap_x_mm:100}]) {
    const project=fixture(),before=structuredClone(project);assert.throws(()=>applyPanelLayout(project,options));assert.deepEqual(project,before);
  }
});

test('too-large fixed label fonts fail without shrinking type or altering the project',()=>{
  const project=fixture(4,{width:20,height:20});project.panels[0].label_font_size_pt=80;const before=structuredClone(project);
  assert.throws(()=>applyPanelLayout(project,{margin_mm:1,columns:2,rows:2}));assert.deepEqual(project,before);
});

test('width adaptation keeps all object geometry inside, source crops and font sizes unchanged',()=>{
  const project=fixture(4);applyPanelLayout(project);
  project.annotations=[{id:'t',type:'text',x_mm:135,y_mm:100,width_mm:38,height_mm:8,text:'Scale',font_size_pt:9},{id:'l',type:'line',x_mm:20,y_mm:110,width_mm:100,height_mm:0,color:'#000'}];
  const crops=structuredClone(project.panels.map(panel=>panel.crop)),font=project.panels.map(panel=>panel.label_font_size_pt);
  const report=resizeFigure(project,85,120);
  assert.equal(report.mode,'fit');assert.ok(report.scale>0&&report.scale<1);assert.deepEqual(project.panels.map(panel=>panel.crop),crops);assert.deepEqual(project.panels.map(panel=>panel.label_font_size_pt),font);assert.equal(project.annotations[0].font_size_pt,9);
  within(compositionBounds(project),project.figure);
  assert.equal(project.annotations[1].height_mm,0);assert.ok(project.annotations[1].width_mm<100);
});

test('fit handles existing off-canvas and rotated content with a preserved physical label size',()=>{
  const project=fixture(1);project.panels[0].x_mm=-20;project.panels[0].y_mm=-10;project.panels[0].rotation=45;
  project.annotations=[{id:'x',type:'rect',x_mm:190,y_mm:130,width_mm:20,height_mm:10}];
  resizeFigure(project,85,80);within(compositionBounds(project),project.figure);assert.equal(project.panels[0].label_font_size_pt,10);
});

test('canvas-only resizing refuses clipping and leaves the entire project untouched',()=>{
  const project=fixture(2);applyPanelLayout(project);const before=structuredClone(project);
  assert.throws(()=>resizeFigure(project,20,20,{mode:'canvas'}));assert.deepEqual(project,before);
  resizeFigure(project,200,150,{mode:'canvas'});assert.deepEqual(project.panels,before.panels);assert.equal(project.figure.width_mm,200);
});

test('reflow adapts independent annotations and reports the need to review their correspondence',()=>{
  const project=fixture(6);applyPanelLayout(project,{columns:3,rows:2});project.annotations=[{id:'note',type:'text',x_mm:20,y_mm:20,width_mm:10,height_mm:5,text:'note',font_size_pt:7}];
  const report=resizeFigure(project,120,180,{mode:'reflow'});assert.equal(report.warnings.length,1);within(compositionBounds(project),project.figure);assert.equal(project.annotations[0].font_size_pt,7);
});

test('impossible text-preserving resize, invalid dimensions and mode all fail atomically',()=>{
  for(const request of [[NaN,100,{}],[85,-1,{}],[5000,100,{}],[85,100,{mode:'stretch'}],[20,20,{}]]) {
    const project=fixture(1);project.annotations=[{id:'t',type:'text',x_mm:10,y_mm:10,width_mm:20,height_mm:5,text:'This fixed-size text cannot fit in twenty millimeters',font_size_pt:12}];
    const before=structuredClone(project);assert.throws(()=>resizeFigure(project,...request));assert.deepEqual(project,before);
  }
});

test('bounds account for rotated panels, fixed caption fonts and signed line endpoints',()=>{
  const panel={id:'p',x_mm:10,y_mm:20,width_mm:40,height_mm:20,rotation:90,label:'A',label_mode:'manual',label_font_size_pt:10};
  const bounds=panelBounds(panel);assert.ok(bounds.top<20);assert.equal(bounds.bottom,50);
  assert.deepEqual(annotationBounds({type:'line',x_mm:50,y_mm:40,width_mm:-20,height_mm:-10}),{x_mm:30,y_mm:30,width_mm:20,height_mm:10,left:30,top:30,right:50,bottom:40});
});

test('multiline splitting matches native export for CRLF, unicode breaks and trailing newlines',()=>{
  assert.deepEqual(textLines('First\r\nSecond\n'),['First','Second']);
  assert.deepEqual(textLines('First\n\n'),['First','']);
  assert.deepEqual(textLines('First\u2028Second\u0085Third'),['First','Second','Third']);
  assert.deepEqual(textLines(''),['']);
});

test('multiline text uses the longest line and includes the last baseline and descenders',()=>{
  const annotation={type:'text',x_mm:10,y_mm:20,width_mm:0,height_mm:0,text:'WW\r\nA\ng',font_size_pt:10};
  const size=10*25.4/72,bounds=annotationBounds(annotation);
  assert.ok(Math.abs(bounds.width_mm-2*1.05*size)<1e-9);
  const lastBaseline=annotation.y_mm+size+2*1.25*size;
  assert.ok(bounds.bottom>=lastBaseline+.25*size);
  assert.ok(bounds.top<=annotation.y_mm);
  const panel={x_mm:10,y_mm:20,width_mm:20,height_mm:1,label_mode:'manual',label:'A\nB\ng',label_font_size_pt:10,title:'Title\nMore\nLast',title_font_size_pt:12};
  const captionBounds=panelBounds(panel);
  assert.ok(captionBounds.bottom>=panel.y_mm-1.8+2*1.25*12*25.4/72);
  assert.ok(captionBounds.bottom>panel.y_mm+panel.height_mm);
});

test('imported centered and right-anchored text participates in fitting at its drawn position',()=>{
  for(const anchor of ['middle','end']) {
    const annotation={id:'anchored',type:'text',x_mm:5,y_mm:20,width_mm:0,height_mm:0,text:'Long title\nMore',font_size_pt:10,anchor};
    const bounds=annotationBounds(annotation);assert.ok(bounds.left<0);
    if(anchor==='middle')assert.ok(Math.abs(5-bounds.left-(bounds.right-5))<1e-9);
    else assert.equal(bounds.right,5);
    const project=fixture(1);project.annotations=[annotation];resizeFigure(project,85,120);
    within(compositionBounds(project),project.figure);assert.equal(project.annotations[0].anchor,anchor);
  }
});

test('layout and fit include imported multiline captions without shrinking physical font sizes',()=>{
  const project=fixture(2,{ratio:20});
  for(const panel of project.panels){panel.title='Treatment\nSecond line\nThird line';panel.title_font_size_pt=12;}
  applyPanelLayout(project,{columns:2,rows:1,margin_mm:0});
  for(const panel of project.panels)within(panelBounds(panel),project.figure);
  project.annotations=[{id:'multiline',type:'text',x_mm:170,y_mm:115,width_mm:0,height_mm:0,text:'First\nSecond\nThird',font_size_pt:10}];
  resizeFigure(project,85,75);
  within(compositionBounds(project),project.figure);
  assert.equal(project.annotations[0].font_size_pt,10);
  assert.ok(project.panels.every(panel=>panel.title_font_size_pt===12));
  const annotation=project.annotations[0],lastBaseline=annotation.y_mm+(1+2*1.25)*10*25.4/72;
  assert.ok(lastBaseline<project.figure.height_mm);
});

test('fixed multiline text that is taller than the target canvas rejects resize atomically',()=>{
  const project=fixture(1);project.annotations=[{id:'tall',type:'text',x_mm:10,y_mm:10,width_mm:0,height_mm:0,text:'A\nB\nC\nD\nE\nF\nG',font_size_pt:20}];
  const before=structuredClone(project);
  assert.throws(()=>resizeFigure(project,85,25));assert.deepEqual(project,before);
});

test('layout settings preserve unrelated composition extensions and pure reads do not initialize objects',()=>{
  const project=fixture(1),before=structuredClone(project);assert.equal(layoutOptions(project).margin_mm,6);assert.deepEqual(project,before);
  project.settings.composition={journal:{id:'custom'},layout:{columns:1,future_layout:42},numbering:{future_label:'saved'}};applyPanelLayout(project);assert.deepEqual(project.settings.composition.journal,{id:'custom'});
  assert.equal(project.settings.composition.layout.future_layout,42);assert.equal(project.settings.composition.numbering.future_label,'saved');
});

test('legacy annotations with omitted optional extents can still be adapted',()=>{
  const project=fixture(1);project.annotations=[{id:'legacy',type:'text',x_mm:10,y_mm:10,text:'Note',font_size_pt:8}];
  resizeFigure(project,85,120);within(compositionBounds(project),project.figure);assert.equal(project.annotations[0].font_size_pt,8);
});

test('seeded mixed-aspect layouts preserve every crop and contain final image-plus-caption bounds',()=>{
  let seed=104729;const random=()=>((seed=(seed*1664525+1013904223)>>>0)/2**32);
  for(let pass=0;pass<50;pass++) {
    const count=1+Math.floor(random()*16),project=fixture(count,{width:180+random()*200,height:140+random()*200});
    for(const panel of project.panels){panel.crop.width=200+random()*1500;panel.crop.height=200+random()*1500;panel.rotation=random()*180-90;}
    const crops=structuredClone(project.panels.map(panel=>panel.crop));
    applyPanelLayout(project,{margin_mm:3+random()*5,gap_x_mm:random()*4,gap_y_mm:random()*4});
    assert.deepEqual(project.panels.map(panel=>panel.crop),crops);
    for(const panel of project.panels)within(panelBounds(panel),project.figure);
    for(let i=0;i<count;i++)for(let j=i+1;j<count;j++)assert.ok(disjoint(panelBounds(project.panels[i]),panelBounds(project.panels[j])));
  }
});
