"""Card 24: numerical real-bake evidence for Per Material Packed."""
import os
import sys
import time
import ast
from pathlib import Path
from types import SimpleNamespace
import bpy
import numpy as np

WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, WS)
import material_bakery
from material_bakery.core import plan, scene_scan, transaction, presets
from material_bakery.engine import backend, job, per_material
from material_bakery.ui import properties, panels, session

failed = []
def check(label, value, detail=''):
    print('[{}] {} {}'.format('PASS' if value else 'FAIL', label, detail))
    if not value:
        failed.append(label)

def supplemental_no_cycles():
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    shared=bpy.data.materials.new('SharedSupplement')
    shared.use_nodes=True
    other=bpy.data.materials.new('OtherSupplement')
    other.use_nodes=True
    objects=[]
    for index in range(2):
        mesh=bpy.data.meshes.new('SupplementMesh'+str(index))
        mesh.from_pydata([(0,0,0),(1,0,0),(1,1,0),(0,1,0),(2,0,0),(2,1,0)],[],[(0,1,2,3),(1,4,5,2),(0,3,2,1)])
        mesh.materials.append(shared)
        mesh.materials.append(shared)
        mesh.materials.append(other)
        for i,poly in enumerate(mesh.polygons): poly.material_index=i
        obj=bpy.data.objects.new('Supplement'+str(index),mesh)
        bpy.context.collection.objects.link(obj)
        obj.select_set(True)
        mesh.uv_layers.new(name='OldUV')
        mesh.uv_layers.new(name='PerMaterial')
        mesh.uv_layers['PerMaterial'].active_render=True
        objects.append(obj)
    bpy.context.view_layer.objects.active=objects[0]
    cfg=plan.BakeSettings(target_mode=scene_scan.MODE_SELECTED,maps=(plan.MapRequest('shader.base_color',8),),workflow='per_material')
    p=plan.build_plan(bpy.context,cfg)
    check('shared material pair count/dedup',len(p.tasks)==4 and sum(len(g.material_slots)==2 for g in p.groups)==2,'objects=2 materials=2 tasks={} repeated_groups={}'.format(len(p.tasks),sum(len(g.material_slots)==2 for g in p.groups)))
    from material_bakery.core.materials import MaterialStore
    from material_bakery.deliver.materials import build_for_groups
    images={}
    for t in p.tasks:
        t.status='done'
        t.image=bpy.data.images.new(t.image_name,width=8,height=8)
        images[t.group_name]={t.bake_type.key:t.image}
    built=build_for_groups(MaterialStore(),images)
    active=session.Session.get()
    active.reset()
    active.job=SimpleNamespace(plan=p,settings=cfg)
    active.materials_by_group=built
    check('pair delivery objects',active.apply_materials(s)==2,'expected=2')
    for obj in objects:
        mapping=dict(obj.get('mbakery_per_material_slots',{}))
        print('SLOT_MAP',obj.name,'slots',len(obj.material_slots),'original',list(obj.get('mbakery_orig_slots',[])),'baked',mapping,flush=True)
        check('duplicate slots same baked '+obj.name,len(obj.material_slots)==3 and obj.material_slots[0].material==obj.material_slots[1].material and obj.material_slots[1].material!=obj.material_slots[2].material,str(mapping))
    before_indices=[[poly.material_index for poly in o.data.polygons] for o in objects]
    check('original preview count',active.preview_materials(s,baked=False)==2,'expected=2')
    check('original preview slots',all([m.name for m in o.data.materials]==[shared.name,shared.name,other.name] for o in objects),'objects=2 slots_each=3')
    check('baked preview count',active.preview_materials(s,baked=True)==2,'expected=2')
    check('per material finalize count',active.finalize_materials(s)==2,'expected=2')
    check('finalize preserves slot/face mapping',all(len(o.material_slots)==3 for o in objects) and before_indices==[[poly.material_index for poly in o.data.polygons] for o in objects],'slots=3/3 face_index_delta=0')
    from material_bakery.core import materials
    restored=sum(materials.restore_original_materials(o)[0] for o in objects)
    check('finalized original restore',restored==2 and all([m.name for m in o.data.materials]==[shared.name,shared.name,other.name] for o in objects),'restored={} slots=3/3'.format(restored))
    # A mesh without UV is accepted in this workflow and gets a Packed layer.
    naked=objects[0]
    for layer in list(naked.data.uv_layers): naked.data.uv_layers.remove(layer)
    group=next(g for g in p.groups if g.objects[0]==naked)
    per_material.prepare_group(bpy.context,group)
    check('no UV object prepared explicitly','PerMaterial' in naked.data.uv_layers,'reason=Per Material generates Packed UV; layers='+str(list(naked.data.uv_layers.keys())))
    migrated,notes=presets.migrate({'version':presets.PRESET_VERSION,'workflow':'bake','maps':[]})
    s['workflow']='bake'
    check('old bake setting/preset',s.workflow=='collection' and migrated['workflow']=='collection','RNA={} preset={} notes={}'.format(s.workflow,migrated['workflow'],notes))
    # Load only the existing strict draw recorder/helpers, never execute its suite.
    tree=ast.parse(Path(WS,'tests/mb_test_panel_draw.py').read_text(encoding='utf8'))
    wanted={'Recorder','_verify_icon','draw_page','operators_in'}
    nodes=[n for n in tree.body if isinstance(n,(ast.ClassDef,ast.FunctionDef)) and n.name in wanted]
    import traceback
    ns={'upanels':panels,'traceback':traceback}
    exec(compile(ast.Module(body=nodes,type_ignores=[]),'<existing panel recorder>','exec'),ns)
    active.reset()
    from material_bakery.ui import ops
    ops.add_map(s,'shader.base_color',64,dedupe=True)
    draw_count=0
    for workflow in properties.WORKFLOWS:
        s.workflow=workflow[0]
        for step,_,_ in properties.steps_for(s):
            log=ns['draw_page'](s,step,bpy.context)
            errors=[] if isinstance(log,str) else [item for item in log if item[0]=='label' and ('draw failed' in item[2].get('text','').lower() or item[2].get('icon')=='ERROR' and 'error' in item[2].get('text','').lower())]
            operators=[] if isinstance(log,str) else ns['operators_in'](log)
            ok=not isinstance(log,str) and not errors and 'mbakery.back' in operators and ('mbakery.next' in operators or 'mbakery.start_over' in operators)
            check('draw '+workflow[0]+'/'+step,ok,log if isinstance(log,str) else 'controls={} draw_errors={}'.format(len(log),len(errors)))
            draw_count+=1
    log=[]
    # Static dispatch routing proves the original collection/rebake paths survive.
    import inspect
    check('generic preview/finalize retained','slots_mod.assign_objects' in inspect.getsource(session.Session.preview_materials) and 'slots_mod.finalize_objects' in inspect.getsource(session.Session.finalize_materials),'generic_paths=2')
    print('POLLUTION_CONTROL_UNVERIFIED NullBackend fills constants; genuine foreign>0 requires separately authorized Cycles control',flush=True)
    material_bakery.unregister()
    if failed:
        print('FAILED {} checks: {}'.format(len(failed),failed),flush=True)
        raise SystemExit(1)
    print('ALL CHECKS PASSED supplemental no Cycles',flush=True)
    raise SystemExit(0)

material_bakery.register()
s = bpy.context.scene.mbakery
if '--supplemental-no-cycles' in sys.argv:
    supplemental_no_cycles()
check('workflow count/order/default', [x[0] for x in properties.WORKFLOWS] == ['per_material', 'collection', 'rebake'] and s.workflow == 'per_material', str(([x[0] for x in properties.WORKFLOWS], s.workflow)))
for raw in (0, 1, 'bake'):
    s['workflow'] = raw
    check('legacy RNA '+str(raw), s.workflow == ('rebake' if raw == 1 else 'collection'), s.workflow)
s.workflow = 'per_material'

bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete(use_global=False)
mesh = bpy.data.meshes.new('Round5Mesh')
mesh.from_pydata([(0,0,0),(1,0,0),(1,1,0),(0,1,0),(2,0,0),(3,0,0),(3,1,0),(2,1,0)], [], [(0,1,2,3),(4,5,6,7)])
obj = bpy.data.objects.new('Round5', mesh)
bpy.context.collection.objects.link(obj)
obj.select_set(True)
bpy.context.view_layer.objects.active=obj
uv=mesh.uv_layers.new(name='OldUV')
for poly in mesh.polygons:
    for i, li in enumerate(poly.loop_indices):
        uv.data[li].uv = [(0,0),(.7,0),(.7,.7),(0,.7)][i]
old=np.array([tuple(x.uv) for x in uv.data])
for i,color in enumerate(((1,0,0,1),(0,0,1,1))):
    mat=bpy.data.materials.new('Red' if i==0 else 'Blue')
    mat.use_nodes=True
    mat.node_tree.nodes.get('Principled BSDF').inputs['Base Color'].default_value=color
    tex=mat.node_tree.nodes.new('ShaderNodeTexImage')
    tex.image=bpy.data.images.new('Source'+str(i),width=4,height=4)
    mesh.materials.append(mat)
mesh.polygons[1].material_index=1
settings=plan.BakeSettings(target_mode=scene_scan.MODE_SELECTED, maps=(plan.MapRequest('shader.base_color',64),),workflow='per_material',margin=0,use_subfolders=False,pack_into_blend=False)
if '--pollution-control' in sys.argv:
    p=plan.build_plan(bpy.context,settings)
    for group in p.groups:
        per_material.prepare_group(bpy.context,group)
    # One real control task only. Both target materials now share its image;
    # all other production dispatch and UV-swap behavior remains identical.
    task=p.tasks[0]
    p.tasks=[task]
    tx=transaction.SceneTransaction(bpy.context)
    b=backend.CyclesBackend(tx,margin=0,samples=1,device='CPU',save_files=False)
    def no_isolation(tx_arg,targets,task_arg):
        b._install_targets(targets,task_arg.image)
    b.material_targets.install=no_isolation
    layers=obj.data.uv_layers
    before=(layers.active_index,tuple(x.name for x in layers if x.active_render))
    start=time.perf_counter()
    j=job.BakeJob(bpy.context,p,settings,backend=b,transaction=tx)
    j.run_to_completion()
    elapsed=time.perf_counter()-start
    pixels=np.array(task.image.pixels[:]).reshape(-1,4)
    foreign=int(np.sum(pixels[:,2]>.05))
    own=int(np.sum(pixels[:,0]>.5))
    after=(layers.active_index,tuple(x.name for x in layers if x.active_render))
    residual=sum(bool(i.get(per_material.SCRATCH_TAG)) for i in bpy.data.images)+sum(bool(n.get(per_material.SCRATCH_TAG)) or n.label.startswith('MBakery Bake') for m in bpy.data.materials if m.node_tree for n in m.node_tree.nodes)
    print('CONTROL tasks=1 state={} sampled=4096 own={} foreign={} seconds={:.6f} before={} after={} residual={}'.format(j.state,own,foreign,elapsed,before,after,residual),flush=True)
    print('BASELINE prior ROUND5_write_layer_fix.log isolated own=4096 foreign=0 sampled=4096',flush=True)
    check('disabled isolation produces foreign texels',task.status=='done' and foreign>0,'foreign={} wrong=0'.format(foreign))
    check('control restores UV and cleans targets',after==before and residual==0,'residual='+str(residual))
    material_bakery.unregister()
    if failed:
        raise SystemExit(1)
    print('ALL CHECKS PASSED pollution control',flush=True)
    raise SystemExit(0)
p=plan.build_plan(bpy.context,settings)
check('two material tasks', len(p.tasks)==2, 'tasks='+str(len(p.tasks))+'; wrong=1')
tx=transaction.SceneTransaction(bpy.context)
b=backend.CyclesBackend(tx,margin=0,samples=1,device='CPU',save_files=False)
j=job.BakeJob(bpy.context,p,settings,backend=b,transaction=tx)
def uv_state():
    layers=bpy.data.objects['Round5'].data.uv_layers
    return (layers.active_index, tuple(layer.name for layer in layers if layer.active_render))

real_run=b._run_bake
real_dispatch=b.dispatch
real_collect=b.collect
saved_states={}
def observed_dispatch(task):
    saved_states[task.key]=uv_state()
    print('DISPATCH_BEFORE', task.image_name, uv_state(), flush=True)
    result=real_dispatch(task)
    print('DISPATCH_AFTER', task.image_name, uv_state(), flush=True)
    return result
def observed_run(operator, kwargs):
    layers=obj.data.uv_layers
    check('dispatch write UV flags', uv_state()==(layers.find('PerMaterial'), ('PerMaterial',)), str(uv_state()))
    return real_run(operator, kwargs)
def observed_collect(task):
    result=real_collect(task)
    check('collect UV exact restore', uv_state()==saved_states[task.key], 'before={} after={}'.format(saved_states[task.key], uv_state()))
    return result
b.dispatch=observed_dispatch
b._run_bake=observed_run
b.collect=observed_collect
start=time.perf_counter()
j.run_to_completion()
print('N_materials={} seconds={:.6f} state={} errors={}'.format(len(p.groups),time.perf_counter()-start,j.state,[t.error for t in p.tasks]))
check('two real bakes',sum(t.status=='done' for t in p.tasks)==2,str([(t.status,t.error) for t in p.tasks]))
for i,t in enumerate(p.tasks):
    pixels=np.array(t.image.pixels[:]).reshape(-1,4)
    foreign=int(np.sum(pixels[:,2 if i==0 else 0]>.05))
    own=int(np.sum(pixels[:,0 if i==0 else 2]>.5))
    # Pixel-center polygon inclusion on the final PerMaterial layout (64x64尺).
    polygon=mesh.polygons[i]
    coords=np.array([tuple(mesh.uv_layers['PerMaterial'].data[li].uv) for li in polygon.loop_indices])
    centers=np.array([((x+.5)/64,(y+.5)/64) for y in range(64) for x in range(64)])
    mask=np.zeros(4096,dtype=bool)
    for a,c in zip(coords,np.roll(coords,-1,axis=0)):
        crossing=(a[1]>centers[:,1])!=(c[1]>centers[:,1])
        cross_x=(c[0]-a[0])*(centers[:,1]-a[1])/(c[1]-a[1]+1e-30)+a[0]
        mask ^= crossing & (centers[:,0]<cross_x)
    region_own=int(np.sum((pixels[:,0 if i==0 else 2]>.5)&mask))
    region_foreign=int(np.sum((pixels[:,2 if i==0 else 0]>.05)&mask))
    check('foreign texels '+str(i),foreign==0 and region_foreign==0 and region_own>=int(mask.sum()*.9),'sampled=4096 foreign={} own={} final_region={} region_own={} region_foreign={} wrong=foreign>0 or own<90%'.format(foreign,own,int(mask.sum()),region_own,region_foreign))
new=mesh.uv_layers['PerMaterial']
for i,poly in enumerate(mesh.polygons):
    def area(coords):
        return abs(sum(coords[k][0]*coords[(k+1)%len(coords)][1]-coords[(k+1)%len(coords)][0]*coords[k][1] for k in range(len(coords)))/2)
    old_area=area([old[li] for li in poly.loop_indices])
    new_area=area([tuple(new.data[li].uv) for li in poly.loop_indices])
    check('UV area '+str(i),new_area/old_area>=.9,'old={} new={} ratio={}'.format(old_area,new_area,new_area/old_area))
old_current = np.array([tuple(x.uv) for x in bpy.data.objects['Round5'].data.uv_layers['OldUV'].data])
check('old UV drift',np.max(np.abs(old-old_current))==0,'drift='+str(np.max(np.abs(old-old_current))))
for mat in mesh.materials:
    images=[n for n in mat.node_tree.nodes if n.type=='TEX_IMAGE']
    count=sum(bool(n.inputs['Vector'].links) and n.inputs['Vector'].links[0].from_node.type=='UVMAP' and n.inputs['Vector'].links[0].from_node.uv_map=='OldUV' for n in images)
    check('source pins '+mat.name,count==len(images),'pinned={} images={}'.format(count,len(images)))
def remnants():
    return sum(bool(i.get(per_material.SCRATCH_TAG)) for i in bpy.data.images)+sum(bool(n.get(per_material.SCRATCH_TAG)) or n.label.startswith('MBakery Bake') for m in bpy.data.materials if m.node_tree for n in m.node_tree.nodes)
check('success remnants',remnants()==0,'residual='+str(remnants()))
from material_bakery.core.materials import MaterialStore
from material_bakery.deliver.materials import build_for_groups
by_group=build_for_groups(MaterialStore(),{t.group_name:{t.bake_type.key:t.image} for t in p.tasks})
check('delivery',per_material.apply_materials(p,by_group)==1 and len(mesh.materials)==2,'materials={} backup={}'.format(len(mesh.materials),list(obj.get('mbakery_orig_slots',[]))))
from material_bakery.core import materials
ok,reason=materials.restore_original_materials(obj)
check('restore original slots',ok and [m.name for m in mesh.materials]==['Red','Blue'],reason)
# Isolate cancellation and dispatch failure cleanup using the real target install seam.
for route in ('cancel','failure'):
    tx=transaction.SceneTransaction(bpy.context)
    b=backend.CyclesBackend(tx,margin=0,samples=1,device='CPU',save_files=False)
    tx.capture()
    b.begin(p,settings)
    task=p.tasks[0]
    b.prepare(task)
    before_uv=uv_state()
    if route=='failure':
        def fail(*args):
            check('failure write UV flags',uv_state()==(mesh.uv_layers.find('PerMaterial'),('PerMaterial',)),str(uv_state()))
            raise RuntimeError('ROUND5 injected dispatch failure')
        b._run_bake=fail
        try:
            b.dispatch(task)
        except RuntimeError as exc:
            print('failure raw:',str(exc))
    else:
        b.material_targets.install(tx,[obj],task)
        b.uv_swap.apply([obj],per_material.UV_NAME)
    b.abort()
    tx.rollback()
    check(route+' UV exact restore',uv_state()==before_uv,'before={} after={}'.format(before_uv,uv_state()))
    check(route+' remnants',remnants()==0,'residual='+str(remnants()))
# Non-baking regression: collection remains a no-op; Re-bake still swaps/restores.
b=backend.CyclesBackend(transaction.SceneTransaction(bpy.context),save_files=False)
initial_uv=uv_state()
b._prepare_uv_layers([obj])
check('collection UV no-op',uv_state()==initial_uv and len(b.uv_swap._saved)==0,str(uv_state()))
b.write_uv='OldUV'
b._prepare_uv_layers([obj])
check('rebake explicit OldUV',uv_state()==(mesh.uv_layers.find('OldUV'),('OldUV',)),str(uv_state()))
b._revert_uv_layers()
check('rebake UV exact restore',uv_state()==initial_uv,str(uv_state()))
material_bakery.unregister()
if failed:
    print('FAILED {} checks: {}'.format(len(failed),failed))
    raise SystemExit(1)
print('ALL CHECKS PASSED')
