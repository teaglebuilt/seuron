from airflow import DAG

from airflow.operators.python_operator import PythonOperator

from airflow.operators.dagrun_operator import TriggerDagRunOperator
from airflow.utils.weight_rule import WeightRule
from airflow.models import Variable

from chunk_iterator import ChunkIterator

from slack_message import slack_message, task_start_alert, task_done_alert, task_retry_alert
from segmentation_op import composite_chunks_batch_op, composite_chunks_overlap_op, composite_chunks_wrap_op, remap_chunks_batch_op
from helper_ops import slack_message_op, scale_up_cluster_op, scale_down_cluster_op, wait_op, mark_done_op, reset_flags_op, reset_cluster_op

from param_default import param_default, default_args, CLUSTER_1_CONN_ID, CLUSTER_2_CONN_ID
from igneous_and_cloudvolume import create_info, downsample_and_mesh, get_info_job, get_eval_job, dataset_resolution
import numpy as np
import json
import urllib
from collections import OrderedDict

def generate_ng_payload(param):
    ng_resolution = dataset_resolution(param["AFF_PATH"], int(param.get("AFF_MIP", 0)))
    seg_resolution = ng_resolution
    layers = OrderedDict()
    if "IMAGE_PATH" in param:
        layers["img"] = {
            "source": "precomputed://"+param["IMAGE_PATH"],
            "type": "image"
        }
        if "IMAGE_SHADER" in param:
            layers["img"]["shader"] = param["IMAGE_SHADER"]

        ng_resolution = dataset_resolution(param["IMAGE_PATH"])

    layers["aff"] = {
        "source": "precomputed://"+param["AFF_PATH"],
        "shader": param.get("AFF_SHADER", "void main() {\n  float r = toNormalized(getDataValue(0));\n  float g = toNormalized(getDataValue(1));\n  float b = toNormalized(getDataValue(2)); \n  emitRGB(vec3(r,g,b));\n}"),
        "type": "image",
        "visible": False
    }

    if "SEM_PATH" in param:
        layers["sem"] = {
            "source": "precomputed://"+param["SEM_PATH"],
            "type": "segmentation",
            "visible": False
        }

    layers["ws"] = {
        "source": "precomputed://"+param["WS_PATH"],
        "type": "segmentation",
        "visible": False
    }

    layers["seg"] = {
        "source": "precomputed://"+param["SEG_PATH"],
        "type": "segmentation"
    }

    if "GT_PATH" in param:
        layers["gt"] = {
            "source": "precomputed://"+param["GT_PATH"],
            "type": "segmentation"
        }

    layers["size"] = {
        "source": "precomputed://"+param["SEG_PATH"]+"/size_map",
        "type": "image"
    }

    bbox = param["BBOX"]

    scale = [seg_resolution[i]/ng_resolution[i] for i in range(3)]
    center = [(bbox[i]+bbox[i+3])/2*scale[i] for i in range(3)]

    navigation = {
        "pose": {
            "position": {
                "voxelSize": ng_resolution,
                "voxelCoordinates": center
            }
        },
        "zoomFactor": 4
    }

    payload = OrderedDict([("layers", layers),("navigation", navigation),("showSlices", False),("layout", "xy-3d")])
    return payload

def generate_link(param, broadcast, **kwargs):
    ng_host = "https://neuromancer-seung-import.appspot.com"
    payload = generate_ng_payload(param)
    ti = kwargs['ti']
    seglist = ti.xcom_pull(task_ids="Check_Segmentation", key="topsegs")
    payload["layers"]["seg"]["hiddenSegments"] = [str(x) for x in seglist]

    url = "neuroglancer link: {host}/#!{payload}".format(
        host=ng_host,
        payload=urllib.parse.quote(json.dumps(payload)))
    slack_message(url, broadcast=broadcast)


dag_manager = DAG("segmentation", default_args=default_args, schedule_interval=None)

dag = dict()

dag["ws"] = DAG("watershed", default_args=default_args, schedule_interval=None)

dag["agg"] = DAG("agglomeration", default_args=default_args, schedule_interval=None)

dag_ws = dag["ws"]
dag_agg = dag["agg"]

Variable.setdefault("param", param_default, deserialize_json=True)
param = Variable.get("param", deserialize_json=True)
image = param["WORKER_IMAGE"]

for p in ["SCRATCH", "WS", "SEG"]:
    path = "{}_PATH".format(p)
    if path not in param:
        param[path] = param["{}_PREFIX".format(p)]+param["NAME"]


def confirm_dag_run(context, dag_run_obj):
    skip_flag = context['params']['skip_flag']
    op = context['params']['op']
    if param.get(skip_flag, False):
        slack_message(":exclamation: Skip {op}".format(op=op))
    else:
        return dag_run_obj


def process_composite_tasks(c, cm, top_mip, params):
    local_batch_mip = batch_mip
    if top_mip < batch_mip:
        local_batch_mip = top_mip

    if c.mip_level() < local_batch_mip:
        return

    short_queue = "atomic"
    long_queue = "composite"

    composite_queue = short_queue if c.mip_level() < high_mip else long_queue+"_"+str(c.mip_level())

    top_tag = str(top_mip)+"_0_0_0"
    tag = str(c.mip_level()) + "_" + "_".join([str(i) for i in c.coordinate()])
    if c.mip_level() > local_batch_mip:
        for stage, op in [("ws", "ws"), ("agg", "me")]:
            generate_chunks[stage][c.mip_level()][tag]=composite_chunks_wrap_op(image, dag[stage], cm, composite_queue, tag, stage, op, params)
            slack_ops[stage][c.mip_level()].set_upstream(generate_chunks[stage][c.mip_level()][tag])
    elif c.mip_level() == local_batch_mip:
        for stage, op in [("ws", "ws"), ("agg", "me")]:
            generate_chunks[stage][c.mip_level()][tag]=composite_chunks_batch_op(image, dag[stage], cm, short_queue, local_batch_mip, tag, stage, op, params)
            if params.get('OVERLAP', False) and stage == 'agg':
                overlap_chunks[tag] = composite_chunks_overlap_op(image, dag[stage], cm, short_queue, tag, params)
                for n in c.neighbours():
                    n_tag = str(n.mip_level()) + "_" + "_".join([str(i) for i in n.coordinate()])
                    if n_tag in generate_chunks[stage][c.mip_level()]:
                        overlap_chunks[tag].set_upstream(generate_chunks[stage][n.mip_level()][n_tag])
                        if n_tag != tag:
                            overlap_chunks[n_tag].set_upstream(generate_chunks[stage][c.mip_level()][tag])
                #slack_ops[stage][c.mip_level()].set_downstream(overlap_chunks[tag])
                slack_ops[stage]['overlap'].set_upstream(overlap_chunks[tag])
            slack_ops[stage][c.mip_level()].set_upstream(generate_chunks[stage][c.mip_level()][tag])
            remap_chunks[stage][tag]=remap_chunks_batch_op(image, dag[stage], cm, short_queue, local_batch_mip, tag, stage, op, params)
            slack_ops[stage]["remap"].set_upstream(remap_chunks[stage][tag])
            generate_chunks[stage][top_mip][top_tag].set_downstream(remap_chunks[stage][tag])
            init[stage].set_downstream(generate_chunks[stage][c.mip_level()][tag])

    if c.mip_level() < top_mip:
        parent_coord = [i//2 for i in c.coordinate()]
        parent_tag = str(c.mip_level()+1) + "_" + "_".join([str(i) for i in parent_coord])
        for stage in ["ws", "agg"]:
            if params.get("OVERLAP", False) and stage == "agg" and c.mip_level() == batch_mip:
                overlap_chunks[tag].set_downstream(generate_chunks[stage][c.mip_level()+1][parent_tag])
            else:
                generate_chunks[stage][c.mip_level()][tag].set_downstream(generate_chunks[stage][c.mip_level()+1][parent_tag])


def generate_batches(param):
    v = ChunkIterator(param["BBOX"], param["CHUNK_SIZE"])
    top_mip = v.top_mip_level()
    batch_mip = 3
    batch_chunks = []
    high_mip_chunks = []
    current_mip = top_mip
    mip_tasks = 0
    if top_mip > batch_mip:
        for c in v:
            if c.mip_level() > batch_mip:
                if current_mip != c.mip_level():
                    if current_mip > batch_mip and mip_tasks > 50:
                        batch_mip = c.mip_level()
                    current_mip = c.mip_level()
                    mip_tasks = 1
                else:
                    mip_tasks += 1
                high_mip_chunks.append(c)
            elif c.mip_level() < batch_mip:
                break
            elif c.mip_level() == batch_mip:
                batch_chunks.append(ChunkIterator(param["BBOX"], param["CHUNK_SIZE"], start_from = [batch_mip]+c.coordinate()))
    else:
        batch_chunks=[v]

    return high_mip_chunks, batch_chunks


def get_evals(param):
    from joblib import Parallel, delayed

    high_mip_chunks, batch_chunks = generate_batches(param)
    contents = Parallel(n_jobs=-2)(delayed(get_eval_job)(sv, param) for sv in batch_chunks)

    content = b''
    for c in contents:
        content+=c

    return content

def evaluate_results(param):
    from io import BytesIO
    import os
    from collections import defaultdict
    from evaluate_segmentation import read_chunk, evaluate_rand, evaluate_voi, find_large_diff
    from igneous_and_cloudvolume import upload_json
    from airflow import configuration as conf
    content = get_evals(param)
    f = BytesIO(content)
    s_i = defaultdict(int)
    t_j = defaultdict(int)
    p_ij = defaultdict(lambda: defaultdict(int))
    payload = generate_ng_payload(param)
    payload['layers']['size']['visible'] = False
    while True:
        if not read_chunk(f, s_i, t_j, p_ij):
            break
    rand_split, rand_merge = evaluate_rand(s_i, t_j, p_ij)
    voi_split, voi_merge = evaluate_voi(s_i, t_j, p_ij)
    seg_pairs = find_large_diff(s_i, t_j, p_ij, payload)
    output = {
        "ng_payload": payload,
        "seg_pairs": seg_pairs
    }


    gs_log_path = conf.get('core', 'remote_log_folder')
    bucket_name = gs_log_path[5:].split('/')[0]
    diff_server = param.get("DIFF_SERVER", "https://diff-dot-neuromancer-seung-import.appspot.com")

    upload_json("gs://"+os.path.join(bucket_name,"diff"), "{}.json".format(param["NAME"]), output)

    msg = '''*Evaluation against ground truth* `{gt_path}`:
rand split: *{rand_split}*
rand merge: *{rand_merge}*
voi split : *{voi_split}*
voi merge : *{voi_merge}*
seg diff: {url}
'''.format(
    gt_path=param["GT_PATH"],
    rand_split=round(abs(rand_split),3),
    rand_merge=round(abs(rand_merge),3),
    voi_split=round(abs(voi_split),3),
    voi_merge=round(abs(voi_merge),3),
    url="{}/{}".format(diff_server, param["NAME"])
    )
    slack_message(msg, broadcast=True)


def plot_histogram(data):
    import math
    import matplotlib.pyplot as plt
    max_bin = math.ceil(math.log10(max(data)))
    plt.hist(data, bins=np.logspace(0, max_bin, max_bin+1))
    plt.xscale('log')
    plt.yscale('log')
    plt.title('Distribution of the segment sizes')
    plt.xlabel('number of supervoxels in the segments')
    plt.ylabel('number of segments')
    plt.savefig('/tmp/hist.png')


def get_infos(param):
    from joblib import Parallel, delayed

    high_mip_chunks, batch_chunks = generate_batches(param)
    content = get_info_job(high_mip_chunks, param)
    contents = Parallel(n_jobs=-2)(delayed(get_info_job)(sv, param) for sv in batch_chunks)

    for c in contents:
        content+=c

    return content


def process_infos(param, **kwargs):
    if param.get("SKIP_AGG", False):
        ti = kwargs['ti']
        ti.xcom_push(key='segcount', value=0)
        ti.xcom_push(key='svcount', value=0)
        ti.xcom_push(key='topsegs', value=[])
        return
    dt_count = np.dtype([('segid', np.uint64), ('count', np.uint64)])
    content = get_infos(param)
    data = np.frombuffer(content, dtype=dt_count)
    plot_histogram(data['count'])
    order = np.argsort(data['count'])[::-1]
    ntops = min(20,len(data))
    msg = '''*Agglomeration Finished*
*{nseg}* segments (*{nsv}* supervoxels)

Largest segments:
{top20list}'''.format(
    nseg=len(data),
    nsv=np.sum(data['count']),
    top20list="\n".join("id: {} ({})".format(data[order[i]][0], data[order[i]][1]) for i in range(ntops))
    )
    slack_message(msg, attachment='/tmp/hist.png')
    ti = kwargs['ti']
    ti.xcom_push(key='segcount', value=len(data))
    ti.xcom_push(key='svcount', value=np.sum(data['count']))
    ti.xcom_push(key='topsegs', value=[data[order[i]][0] for i in range(ntops)])


if "BBOX" in param and "CHUNK_SIZE" in param: #and "AFF_MIP" in param:
    data_bbox = param["BBOX"]

    chunk_size = param["CHUNK_SIZE"]


    #data_bbox = [126280+256, 64280+256, 20826-200, 148720-256, 148720-256, 20993]
    starting_msg ='''*Start Segmenting {name}*
    Affinity map: `{aff}`
    Affinity mip level: {mip}
    Bounding box: [{bbox}]'''.format(
        name = param["NAME"],
        aff = param["AFF_PATH"],
        bbox = ", ".join(str(x) for x in param["BBOX"]),
        mip = param.get("AFF_MIP",0)
    )

    ending_msg = '''*Finish Segmenting {name}*
    Watershed layer: `{ws}`
    Segmentation Layer: `{seg}`'''.format(
        name = param["NAME"],
        ws = param["WS_PATH"],
        seg = param["SEG_PATH"]
    )

    no_rescale_msg = ":exclamation: Cannot rescale cluster"
    rescale_message = ":heavy_check_mark: Rescaled cluster {} to {} instances"

    starting_op = slack_message_op(dag_manager, "start", starting_msg)
    ending_op = slack_message_op(dag_manager, "end", ending_msg)

    reset_flags = reset_flags_op(dag_manager, param)

    init = dict()

    init["ws"] = PythonOperator(
        task_id = "Init_Watershed",
        python_callable=create_info,
        op_args = ["ws", param],
        default_args=default_args,
        on_success_callback=task_start_alert,
        on_retry_callback=task_retry_alert,
        weight_rule=WeightRule.ABSOLUTE,
        dag=dag["ws"],
        queue = "manager"
    )

    init["agg"] = PythonOperator(
        task_id = "Init_Agglomeration",
        python_callable=create_info,
        op_args = ["agg", param],
        default_args=default_args,
        on_success_callback=task_start_alert,
        on_retry_callback=task_retry_alert,
        weight_rule=WeightRule.ABSOLUTE,
        dag=dag["agg"],
        queue = "manager"
    )


    generate_chunks = {
        "ws": {},
        "agg": {}
    }

    overlap_chunks = {}

    remap_chunks = {
        "ws": {},
        "agg": {}
    }

    slack_ops = {
        "ws": {},
        "agg": {}
    }

    scaling_ops = {
        "ws": {},
        "agg": {}
    }

    triggers = dict()
    wait = dict()
    mark_done = dict()

    triggers["ws"] = TriggerDagRunOperator(
        task_id="trigger_ws",
        trigger_dag_id="watershed",
        python_callable=confirm_dag_run,
        params={'skip_flag': "SKIP_WS",
                'op': "watershed"},
        queue="manager",
        dag=dag_manager
    )

    wait["ws"] = wait_op(dag_manager, "ws_done")

    mark_done["ws"] = mark_done_op(dag["ws"], "ws_done")

    triggers["agg"] = TriggerDagRunOperator(
        task_id="trigger_agg",
        trigger_dag_id="agglomeration",
        python_callable=confirm_dag_run,
        params={'skip_flag': "SKIP_AGG",
                'op': "agglomeration"},
        queue="manager",
        dag=dag_manager
    )

    wait["agg"] = wait_op(dag_manager, "agg_done")

    mark_done["agg"] = mark_done_op(dag["agg"], "agg_done")

    v = ChunkIterator(data_bbox, chunk_size)
    top_mip = v.top_mip_level()
    batch_mip = param.get("BATCH_MIP", 3)
    high_mip = param.get("HIGH_MIP", 5)
    local_batch_mip = batch_mip


    check_seg = PythonOperator(
        task_id="Check_Segmentation",
        python_callable=process_infos,
        provide_context=True,
        op_args=[param],
        default_args=default_args,
        dag=dag_manager,
        queue="manager"
    )


    cm = ["param"]
    if "MOUNT_SECRETES" in param:
        cm += param["MOUNT_SECRETES"]

    if top_mip < batch_mip:
        local_batch_mip = top_mip

    if top_mip == batch_mip:
        param["OVERLAP"] = False

    if param.get("OVERLAP", False):
        slack_ops['agg']['overlap'] = slack_message_op(dag['agg'], "overlap_"+str(batch_mip), ":heavy_check_mark: {} MIP {} finished".format("overlapped agglomeration at", batch_mip))

    for c in v:
        if c.mip_level() < local_batch_mip:
            break
        else:
            for k in ["ws","agg"]:
                if c.mip_level() not in generate_chunks[k]:
                    generate_chunks[k][c.mip_level()] = {}

                if c.mip_level() not in slack_ops[k]:
                    slack_ops[k][c.mip_level()] = slack_message_op(dag[k], k+str(c.mip_level()), ":heavy_check_mark: {}: MIP {} finished".format(k, c.mip_level()))
                    if c.mip_level() == local_batch_mip:
                        slack_ops[k]["remap"] = slack_message_op(dag[k], "remap_{}".format(k), ":heavy_check_mark: {}: Remaping finished".format(k))
                        slack_ops[k]["remap"] >> mark_done[k]
            process_composite_tasks(c, cm, top_mip, param)

    cluster1_size = len(remap_chunks["ws"])


    if cluster1_size >= 100:
        reset_cluster_after_ws = reset_cluster_op(dag['ws'], "ws", CLUSTER_1_CONN_ID, 20)
        slack_ops['ws']['remap'] >> reset_cluster_after_ws


    scaling_global_start = scale_up_cluster_op(dag_manager, "global_start", CLUSTER_1_CONN_ID, 20, cluster1_size, "manager")

    scaling_global_finish = scale_down_cluster_op(dag_manager, "global_finish", CLUSTER_1_CONN_ID, 0, "manager")

    igneous_task = PythonOperator(
        task_id = "Downsample_and_Mesh",
        python_callable=downsample_and_mesh,
        op_args = [param,],
        default_args=default_args,
        on_success_callback=task_done_alert,
        on_retry_callback=task_retry_alert,
        dag=dag_manager,
        queue = "manager"
    )

    scaling_igneous_finish = scale_down_cluster_op(dag_manager, "igneous_finish", "igneous", 0, "manager")

    starting_op >> reset_flags >> triggers["ws"] >> wait["ws"] >> triggers["agg"] >> wait["agg"] >> igneous_task >> ending_op
    reset_flags >> scaling_global_start
    igneous_task >> scaling_igneous_finish
    wait["agg"] >> scaling_global_finish
    wait["agg"] >> check_seg

    if "GT_PATH" in param:
        evaluation_task = PythonOperator(
            task_id = "Evaluate_Segmentation",
            python_callable=evaluate_results,
            op_args = [param,],
            default_args=default_args,
            dag=dag_manager,
            queue = "manager"
        )
        nglink_task = PythonOperator(
            task_id = "Generate_neuroglancer_link",
            provide_context=True,
            python_callable=generate_link,
            op_args = [param, False],
            default_args=default_args,
            dag=dag_manager,
            queue = "manager"
        )
        [check_seg, igneous_task] >> nglink_task >> ending_op
        igneous_task >> evaluation_task >> ending_op
    else:
        nglink_task = PythonOperator(
            task_id = "Generate_neuroglancer_link",
            provide_context=True,
            python_callable=generate_link,
            op_args = [param, True],
            default_args=default_args,
            dag=dag_manager,
            queue = "manager"
        )
        [check_seg, igneous_task] >> nglink_task >> ending_op


    if min(high_mip, top_mip) - batch_mip > 2:
        for stage in ["ws", "agg"]:
            dsize = len(generate_chunks[stage][batch_mip+2])*2
            scaling_ops[stage]["extra_down"] = scale_down_cluster_op(dag[stage], stage, CLUSTER_1_CONN_ID, dsize, "manager")
            scaling_ops[stage]["extra_down"].set_upstream(slack_ops[stage][batch_mip+1])

    if top_mip >= high_mip:
        for stage in ["ws", "agg"]:
            scaling_ops[stage]["down"] = scale_down_cluster_op(dag[stage], stage, CLUSTER_1_CONN_ID, 0, "manager")
            scaling_ops[stage]["down"].set_upstream(slack_ops[stage][high_mip-1])


            cluster2_size = max(1, len(generate_chunks[stage][high_mip])//8)
            scaling_ops[stage]["up_long"] = scale_up_cluster_op(dag[stage], stage+"_long", CLUSTER_2_CONN_ID, 2, cluster2_size, "manager")

            for k in generate_chunks[stage][high_mip-1]:
                scaling_ops[stage]["up_long"].set_upstream(generate_chunks[stage][high_mip-1][k])

            scaling_ops[stage]["down_long"] = scale_down_cluster_op(dag[stage], stage+"_long", CLUSTER_2_CONN_ID, 0, "manager")
            scaling_ops[stage]["down_long"].set_upstream(slack_ops[stage][top_mip])

    if min(high_mip, top_mip) - batch_mip >= 2 or top_mip >= high_mip:
        for stage in ["ws", "agg"]:
            scaling_ops[stage]["up"] = scale_up_cluster_op(dag[stage], stage, CLUSTER_1_CONN_ID, 20, cluster1_size, "manager")
            scaling_ops[stage]["up"].set_upstream(slack_ops[stage][top_mip])

