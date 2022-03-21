import argparse
import logging
import os
import shutil
import subprocess
from subprocess import CalledProcessError
import traceback
from typing import Any, List, Optional, Set, Tuple

import yaml

from mir.commands import base
from mir.protos import mir_command_pb2 as mirpb
from mir.tools import checker, class_ids, context, data_exporter, mir_storage_ops, revs_parser
from mir.tools import settings as mir_settings, utils as mir_utils
from mir.tools.command_run_in_out import command_run_in_out
from mir.tools.code import MirCode
from mir.tools.errors import MirRuntimeError


# private: post process
def _process_model_storage(out_root: str, model_upload_location: str, executor_config: dict,
                           task_context: dict) -> Tuple[str, float, Optional[mir_utils.ModelStorage]]:
    """
    find and save models
    Returns:
        model hash, model mAP and ModelStorage
    """
    out_model_dir = os.path.join(out_root, "models")
    model_paths, model_mAP = _find_models(out_model_dir)
    if not model_paths:
        # if have no models
        return '', model_mAP, None

    model_storage = mir_utils.ModelStorage(executor_config=executor_config,
                                           task_context=dict(**task_context,
                                                             mAP=model_mAP,
                                                             type=mirpb.TaskType.TaskTypeTraining),
                                           models=[os.path.basename(model_path) for model_path in model_paths])
    model_sha1 = mir_utils.pack_and_copy_models(model_storage=model_storage,
                                                model_dir_path=out_model_dir,
                                                model_location=model_upload_location)

    return model_sha1, model_mAP, model_storage


def _find_models(model_root: str) -> Tuple[List[str], float]:
    """
    find models in `model_root`, and returns model names and mAP

    Args:
        model_root (str): model root

    Returns:
        Tuple[List[str], float]: list of model names and map
    """
    model_names = []
    model_mAP = 0.0

    result_yaml_path = os.path.join(model_root, "result.yaml")
    try:
        with open(result_yaml_path, "r") as f:
            yaml_obj = yaml.safe_load(f.read())
            model_names = yaml_obj["model"]
            model_mAP = float(yaml_obj["map"])
    except FileNotFoundError:
        logging.warning(traceback.format_exc())
        return [], 0.0

    return ([os.path.join(model_root, os.path.basename(name)) for name in model_names], model_mAP)


def _upload_model_pack(model_pack_path: str, dest_path: str) -> bool:
    if not model_pack_path or not dest_path:
        raise MirRuntimeError(error_code=MirCode.RC_CMD_INVALID_ARGS,
                              error_message='invalid model_pack_path or dest_path')

    shutil.copyfile(model_pack_path, dest_path)
    return True


def _update_mir_tasks(mir_root: str, src_rev_tid: revs_parser.TypRevTid, dst_rev_tid: revs_parser.TypRevTid,
                      model_sha1: str, mAP: float, model_storage: Optional[mir_utils.ModelStorage], task_ret_code: int,
                      task_err_msg: str) -> mirpb.MirTasks:
    """
    add a new mir single task into mir_tasks from branch base_branch, and save it to a new branch: dst_branch
    """
    logging.info("creating task id: {}, model hash: {}, mAP: {}".format(dst_rev_tid.tid, model_sha1, mAP))

    task_parameters = model_storage.task_context.get(mir_settings.TASK_CONTEXT_PARAMETERS_KEY,
                                                     '') if model_storage else ''
    mir_tasks: mirpb.MirTasks = mir_storage_ops.MirStorageOps.load_single(mir_root=mir_root,
                                                                          mir_branch=src_rev_tid.rev,
                                                                          mir_task_id=src_rev_tid.tid,
                                                                          ms=mirpb.MirStorage.MIR_TASKS)
    mir_storage_ops.update_mir_tasks(mir_tasks=mir_tasks,
                                     task_type=mirpb.TaskType.TaskTypeTraining,
                                     task_id=dst_rev_tid.tid,
                                     message='training',
                                     model_mAP=mAP,
                                     model_hash=model_sha1,
                                     return_code=task_ret_code,
                                     return_msg=task_err_msg,
                                     args=(yaml.safe_dump(model_storage.as_dict()) if model_storage else ''),
                                     task_parameters=task_parameters)
    return mir_tasks


# private: process
def _run_train_cmd(cmd: List[str], out_log_path: str) -> int:
    """
    invoke training command

    Args:
        cmd (str): command
        out_log_path (str): path of log file

    Returns:
        int: MirCode.RC_OK if success

    Raises:
        Exception: if out_log_path can not open for append, or cmd returned non-zero code
    """
    logging.info(f"training with cmd: {cmd}")
    logging.info(f"out log path: {out_log_path}")
    with open(out_log_path, 'a') as f:
        # run and wait, if non-zero value returned, raise
        subprocess.run(cmd, check=True, stdout=f, stderr=f, text=True)

    return MirCode.RC_OK


# private: pre process
def _generate_config(executor_config: Any, out_config_path: str, task_id: str,
                     pretrained_model_params: List[str]) -> dict:
    executor_config["task_id"] = task_id
    if pretrained_model_params:
        executor_config['pretrained_model_params'] = pretrained_model_params
    elif 'pretrained_model_params' in executor_config:
        del executor_config['pretrained_model_params']

    logging.info("container config: {}".format(executor_config))

    with open(out_config_path, "w") as f:
        yaml.dump(executor_config, f)

    return executor_config


def _get_shm_size(executor_config: dict) -> str:
    if 'shm_size' not in executor_config:
        return '16G'
    return executor_config['shm_size']


def _prepare_pretrained_models(model_location: str, model_hash: str, dst_model_dir: str,
                               class_names: List[str]) -> List[str]:
    """
    prepare pretrained models
    * extract models to dst_model_dir
    * compare class names
    * returns model file names

    Args:
        model_location (str): model location
        model_hash (str): model package hash
        dst_model_dir (str): dir where you want to extract model files to
        class_names (List[str]): class names for this training

    Returns:
        List[str]: model names
    """
    if not model_hash:
        return []
    model_storage = mir_utils.prepare_model(model_location=model_location,
                                            model_hash=model_hash,
                                            dst_model_path=dst_model_dir)

    # check class names
    if model_storage.class_names != class_names:
        raise MirRuntimeError(
            error_code=MirCode.RC_CMD_INVALID_ARGS,
            error_message=f"class names mismatch: pretrained: {model_storage.class_names}, current: {class_names}")

    return model_storage.models


def _get_task_parameters(config: dict) -> str:
    return config.get(mir_settings.TASK_CONTEXT_KEY, {}).get(mir_settings.TASK_CONTEXT_PARAMETERS_KEY, '')


class CmdTrain(base.BaseCommand):
    def run(self) -> int:
        logging.debug("command train: %s", self.args)

        return CmdTrain.run_with_args(work_dir=self.args.work_dir,
                                      model_upload_location=self.args.model_path,
                                      pretrained_model_hash=self.args.model_hash,
                                      src_revs=self.args.src_revs,
                                      dst_rev=self.args.dst_rev,
                                      mir_root=self.args.mir_root,
                                      media_location=self.args.media_location,
                                      tensorboard_dir=self.args.tensorboard_dir,
                                      executor=self.args.executor,
                                      executor_instance=self.args.executor_instance,
                                      config_file=self.args.config_file)

    @staticmethod
    @command_run_in_out
    def run_with_args(work_dir: str,
                      model_upload_location: str,
                      pretrained_model_hash: str,
                      executor: str,
                      executor_instance: str,
                      src_revs: str,
                      dst_rev: str,
                      config_file: Optional[str],
                      tensorboard_dir: str,
                      mir_root: str = '.',
                      media_location: str = '') -> int:

        if not model_upload_location:
            logging.error("empty --model-location, abort")
            return MirCode.RC_CMD_INVALID_ARGS
        src_typ_rev_tid = revs_parser.parse_single_arg_rev(src_revs, need_tid=False)
        dst_typ_rev_tid = revs_parser.parse_single_arg_rev(dst_rev, need_tid=True)
        if not work_dir:
            logging.error("empty work_dir, abort")
            return MirCode.RC_CMD_INVALID_ARGS
        if not config_file:
            logging.warning('empty --config-file, abort')
            return MirCode.RC_CMD_INVALID_ARGS
        if not os.path.isfile(config_file):
            logging.error(f"invalid --config-file {config_file}, not a file, abort")
            return MirCode.RC_CMD_INVALID_ARGS

        return_code = checker.check(mir_root,
                                    [checker.Prerequisites.IS_INSIDE_MIR_REPO, checker.Prerequisites.HAVE_LABELS])
        if return_code != MirCode.RC_OK:
            return return_code

        with open(config_file, "r") as f:
            config = yaml.safe_load(f)

        task_parameters = _get_task_parameters(config)
        if not isinstance(task_parameters, str):
            raise MirRuntimeError(
                error_code=MirCode.RC_CMD_INVALID_ARGS,
                error_message=f"invalid {mir_settings.TASK_CONTEXT_PARAMETERS_KEY} in config: {config}")
        if mir_settings.EXECUTOR_CONFIG_KEY not in config:
            raise MirRuntimeError(
                error_code=MirCode.RC_CMD_INVALID_ARGS,
                error_message=f"invalid config file: {config_file}, needs: {mir_settings.EXECUTOR_CONFIG_KEY}")

        executor_config = config[mir_settings.EXECUTOR_CONFIG_KEY]

        class_names = executor_config.get('class_names', [])
        if not class_names:
            raise MirRuntimeError(error_code=MirCode.RC_CMD_INVALID_ARGS,
                                  error_message=f"no class_names in config file: {config_file}")
        if len(set(class_names)) != len(class_names):
            raise MirRuntimeError(error_code=MirCode.RC_CMD_INVALID_ARGS,
                                  error_message=f"dumplicate class names in class_names: {class_names}")

        task_id = dst_typ_rev_tid.tid
        if not executor_instance:
            executor_instance = f"default-training-{task_id}"
        if not tensorboard_dir:
            tensorboard_dir = os.path.join(work_dir, 'out', 'tensorboard')

        # if have model_hash, export model
        pretrained_model_names = _prepare_pretrained_models(model_location=model_upload_location,
                                                            model_hash=pretrained_model_hash,
                                                            dst_model_dir=os.path.join(work_dir, 'in', 'models'),
                                                            class_names=class_names)

        # get train_ids, val_ids, test_ids
        train_ids = set()  # type: Set[str]
        val_ids = set()  # type: Set[str]
        test_ids = set()  # type: Set[str]
        unused_ids = set()  # type: Set[str]
        mir_datas = mir_storage_ops.MirStorageOps.load(mir_root=mir_root,
                                                       mir_branch=src_typ_rev_tid.rev,
                                                       mir_task_id=src_typ_rev_tid.tid,
                                                       mir_storages=[mirpb.MirStorage.MIR_METADATAS])
        mir_metadatas: mirpb.MirMetadatas = mir_datas[mirpb.MirStorage.MIR_METADATAS]
        for asset_id, asset_attr in mir_metadatas.attributes.items():
            if asset_attr.tvt_type == mirpb.TvtTypeTraining:
                train_ids.add(asset_id)
            elif asset_attr.tvt_type == mirpb.TvtTypeValidation:
                val_ids.add(asset_id)
            elif asset_attr.tvt_type == mirpb.TvtTypeTest:
                test_ids.add(asset_id)
            else:
                unused_ids.add(asset_id)
        if not train_ids:
            logging.error("no training set; abort")
            return MirCode.RC_CMD_INVALID_ARGS
        if not val_ids:
            logging.error("no validation set; abort")
            return MirCode.RC_CMD_INVALID_ARGS

        if not unused_ids:
            logging.info(f"training: {len(train_ids)}, validation: {len(val_ids)}, test: {len(test_ids)}")
        else:
            logging.warning(f"training: {len(train_ids)}, validation: {len(val_ids)}, test: {len(test_ids)}, "
                            f"unused: {len(unused_ids)}")

        # export
        logging.info("exporting assets")
        os.makedirs(work_dir, exist_ok=True)
        work_dir_in = os.path.join(work_dir, "in")
        work_dir_out = os.path.join(work_dir, "out")
        os.makedirs(work_dir_in, exist_ok=True)
        os.makedirs(work_dir_out, exist_ok=True)
        os.makedirs(tensorboard_dir, exist_ok=True)

        # type names to type ids
        # ['cat', 'person'] -> [4, 2]
        cls_mgr = class_ids.ClassIdManager(mir_root=mir_root)
        type_ids_list = cls_mgr.id_for_names(class_names)
        if not type_ids_list:
            logging.info(f"type ids empty, please check config file: {config_file}")
            return MirCode.RC_CMD_INVALID_ARGS

        if not context.check_class_ids(mir_root=mir_root, current_class_ids=type_ids_list):
            raise MirRuntimeError(error_code=MirCode.RC_CMD_INVALID_ARGS, error_message='project class ids mismatch')

        type_id_idx_mapping = {type_id: index for (index, type_id) in enumerate(type_ids_list)}

        # export train set
        work_dir_in_train = os.path.join(work_dir_in, 'train')
        if os.path.isdir(work_dir_in_train):
            shutil.rmtree(work_dir_in_train)
        data_exporter.export(mir_root=mir_root,
                             assets_location=media_location,
                             class_type_ids=type_id_idx_mapping,
                             asset_ids=train_ids,
                             asset_dir=work_dir_in_train,
                             annotation_dir=work_dir_in_train,
                             need_ext=True,
                             need_id_sub_folder=False,
                             base_branch=src_typ_rev_tid.rev,
                             base_task_id=src_typ_rev_tid.tid,
                             format_type=data_exporter.ExportFormat.EXPORT_FORMAT_ARK,
                             index_file_path=os.path.join(work_dir_in_train, 'index.tsv'),
                             index_prefix='/in/train')

        # export validation set
        work_dir_in_val = os.path.join(work_dir_in, 'val')
        if os.path.isdir(work_dir_in_val):
            shutil.rmtree(work_dir_in_val)
        data_exporter.export(mir_root=mir_root,
                             assets_location=media_location,
                             class_type_ids=type_id_idx_mapping,
                             asset_ids=val_ids,
                             asset_dir=work_dir_in_val,
                             annotation_dir=work_dir_in_val,
                             need_ext=True,
                             need_id_sub_folder=False,
                             base_branch=src_typ_rev_tid.rev,
                             base_task_id=src_typ_rev_tid.tid,
                             format_type=data_exporter.ExportFormat.EXPORT_FORMAT_ARK,
                             index_file_path=os.path.join(work_dir_in_val, 'index.tsv'),
                             index_prefix='/in/val')

        # export test set (if we have)
        if test_ids:
            work_dir_in_test = os.path.join(work_dir_in, 'test')
            if os.path.isdir(work_dir_in_test):
                shutil.rmtree(work_dir_in_test)
            data_exporter.export(mir_root=mir_root,
                                 assets_location=media_location,
                                 class_type_ids=type_id_idx_mapping,
                                 asset_ids=test_ids,
                                 asset_dir=work_dir_in_test,
                                 annotation_dir=work_dir_in_test,
                                 need_ext=True,
                                 need_id_sub_folder=False,
                                 base_branch=src_typ_rev_tid.rev,
                                 base_task_id=src_typ_rev_tid.tid,
                                 format_type=data_exporter.ExportFormat.EXPORT_FORMAT_ARK,
                                 index_file_path=os.path.join(work_dir_in_test, 'index.tsv'),
                                 index_prefix='/in/test')

        logging.info("starting train docker container")

        available_gpu_id = config.get(mir_settings.TASK_CONTEXT_KEY, {}).get('available_gpu_id', '')

        # generate configs
        out_config_path = os.path.join(work_dir_in, "config.yaml")
        executor_config = _generate_config(
            executor_config=executor_config,
            out_config_path=out_config_path,
            task_id=task_id,
            pretrained_model_params=[os.path.join('/in/models', name) for name in pretrained_model_names])

        # start train docker and wait
        path_binds = []
        path_binds.append(f"-v{work_dir_in}:/in")
        path_binds.append(f"-v{work_dir_out}:/out")
        path_binds.append(f"-v{tensorboard_dir}:/out/tensorboard")
        shm_size = _get_shm_size(executor_config=executor_config)

        cmd = ['nvidia-docker', 'run', '--rm', f"--shm-size={shm_size}"]
        cmd.extend(path_binds)
        if available_gpu_id:
            cmd.extend(['--gpus', f"\"device={available_gpu_id}\""])
        cmd.extend(['--user', f"{os.getuid()}:{os.getgid()}"])
        cmd.extend(['--name', f"{executor_instance}"])
        cmd.append(executor)

        task_code = MirCode.RC_OK
        task_error_msg = ''
        try:
            _run_train_cmd(cmd, out_log_path=os.path.join(work_dir_out, 'ymir-executor-out.log'))
        except CalledProcessError as e:
            logging.warning(f"training exception: {e}")
            # don't exit, proceed if model exists
            task_code = MirCode.RC_CMD_CONTAINER_ERROR
            task_error_msg = str(e)

        # gen task_context
        task_context = {
            'src_revs': src_revs,
            'dst_rev': dst_rev,
            'executor': executor,
            mir_settings.PRODUCER_KEY: mir_settings.PRODUCER_NAME,
            mir_settings.TASK_CONTEXT_PARAMETERS_KEY: task_parameters
        }

        # save model
        logging.info("saving models")
        model_sha1, model_mAP, model_storage = _process_model_storage(out_root=work_dir_out,
                                                                      model_upload_location=model_upload_location,
                                                                      executor_config=executor_config,
                                                                      task_context=task_context)

        # update metadatas and task with finish state and model hash
        mir_tasks = _update_mir_tasks(mir_root=mir_root,
                                      src_rev_tid=src_typ_rev_tid,
                                      dst_rev_tid=dst_typ_rev_tid,
                                      model_sha1=model_sha1,
                                      mAP=model_mAP,
                                      model_storage=model_storage,
                                      task_ret_code=task_code,
                                      task_err_msg=task_error_msg)

        if task_code != MirCode.RC_OK:
            raise MirRuntimeError(error_code=task_code,
                                  error_message=task_error_msg,
                                  needs_new_commit=True,
                                  mir_tasks=mir_tasks)

        mir_storage_ops.MirStorageOps.save_and_commit(mir_root=mir_root,
                                                      mir_branch=dst_typ_rev_tid.rev,
                                                      task_id=dst_typ_rev_tid.tid,
                                                      his_branch=src_typ_rev_tid.rev,
                                                      mir_datas={mirpb.MirStorage.MIR_TASKS: mir_tasks},
                                                      commit_message=dst_typ_rev_tid.tid)

        logging.info("training done")

        return MirCode.RC_OK


def bind_to_subparsers(subparsers: argparse._SubParsersAction, parent_parser: argparse.ArgumentParser) -> None:
    train_arg_parser = subparsers.add_parser("train",
                                             parents=[parent_parser],
                                             description="use this command to train current workspace",
                                             help="train current workspace")
    train_arg_parser.add_argument("--model-location",
                                  required=True,
                                  dest="model_path",
                                  type=str,
                                  help="storage place (upload location) to store packed model")
    train_arg_parser.add_argument("--media-location",
                                  required=True,
                                  dest="media_location",
                                  type=str,
                                  help="media storage location for models")
    train_arg_parser.add_argument('--model-hash',
                                  dest='model_hash',
                                  type=str,
                                  required=False,
                                  help='model hash to be used')
    train_arg_parser.add_argument("-w", required=True, dest="work_dir", type=str, help="work place for training")
    train_arg_parser.add_argument("--executor",
                                  required=True,
                                  dest="executor",
                                  type=str,
                                  help="docker image name for training")
    train_arg_parser.add_argument('--executor-instance',
                                  required=False,
                                  dest='executor_instance',
                                  type=str,
                                  help='docker container name for training')
    train_arg_parser.add_argument("--src-revs",
                                  dest="src_revs",
                                  type=str,
                                  required=True,
                                  help="rev@bid: source rev and base task id")
    train_arg_parser.add_argument("--dst-rev",
                                  dest="dst_rev",
                                  type=str,
                                  required=True,
                                  help="rev@tid: destination branch name and task id")
    train_arg_parser.add_argument("--config-file",
                                  dest="config_file",
                                  type=str,
                                  required=True,
                                  help="path to executor config file")
    train_arg_parser.add_argument("--tensorboard",
                                  dest="tensorboard_dir",
                                  type=str,
                                  required=False,
                                  help="tensorboard log directory")
    train_arg_parser.set_defaults(func=CmdTrain)
