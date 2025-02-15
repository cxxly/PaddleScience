# Copyright (c) 2022 PaddlePaddle Authors. All Rights Reserved.
# 
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
#     http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import numpy as np
import os
import paddlescience as psci
import paddle
import os
import wget
import zipfile
from paddlescience.solver.utils import l2_norm_square, compute_bc_loss, compute_eq_loss, compile_and_convert_back_to_program, create_inputs_var, create_labels_var, convert_to_distributed_program, data_parallel_partition
from paddle.distributed.passes import new_pass, PassManager
import time

cfg = psci.utils.parse_args()

if cfg is not None:
    # Geometry
    npoints = cfg['Geometry']['npoints']
    seed_num = cfg['Geometry']['seed']
    sampler_method = cfg['Geometry']['sampler_method']
    # Network
    epochs = cfg['Global']['epochs']
    num_layers = cfg['Model']['num_layers']
    hidden_size = cfg['Model']['hidden_size']
    activation = cfg['Model']['activation']
    # Optimizer
    learning_rate = cfg['Optimizer']['lr']['learning_rate']
    # Post-processing
    solution_filename = cfg['Post-processing']['solution_filename']
    vtk_filename = cfg['Post-processing']['vtk_filename']
    checkpoint_path = cfg['Post-processing']['checkpoint_path']
else:
    # config
    psci.config.enable_static()
    psci.config.enable_prim()
    # Geometry
    npoints = [200, 50, 4]
    seed_num = 1
    sampler_method = "uniform"
    # Network
    epochs = 2000
    num_layers = 10
    hidden_size = 50
    activation = 'tanh'
    # Optimizer
    learning_rate = 0.001
    # Post-processing
    solution_filename = 'output_cylinder3d_unsteady'
    vtk_filename = "train_cylinder_unsteady_re100/cylinder3d_train_rslt_"
    checkpoint_path = 'checkpoint/cylinder3d_model_'

paddle.seed(seed_num)
np.random.seed(seed_num)

# define start time and time step
start_time = 100
time_step = 1


# load real data 
def GetRealPhyInfo(time, need_info=None):
    # if real data don't exist, you need to download it.
    if os.path.exists('./openfoam_cylinder_re100') == False:
        data_set = 'https://dataset.bj.bcebos.com/PaddleScience/cylinder3D/openfoam_cylinder_re100/cylinder3d_openfoam_re100.zip'
        wget.download(data_set)
        with zipfile.ZipFile('cylinder3d_openfoam_re100.zip', 'r') as zip_ref:
            zip_ref.extractall('openfoam_cylinder_re100')
    real_data = np.load("openfoam_cylinder_re100/flow_re100_" + str(
        int(time)) + "_xyzuvwp.npy")
    real_data = real_data.astype(np.float32)
    if need_info == 'cord':
        return real_data[:, 0:3]
    elif need_info == 'physic':
        return real_data[:, 3:7]
    else:
        return real_data


cc = (0.0, 0.0)
cr = 0.5
geo = psci.geometry.CylinderInCube(
    origin=(-8, -8, -2), extent=(25, 8, 2), circle_center=cc, circle_radius=cr)

geo.add_boundary(name="left", criteria=lambda x, y, z: abs(x + 8.0) < 1e-4)
geo.add_boundary(name="right", criteria=lambda x, y, z: abs(x - 25.0) < 1e-4)
geo.add_boundary(
    name="circle",
    criteria=lambda x, y, z: ((x - cc[0])**2 + (y - cc[1])**2 - cr**2) < 1e-4)

# discretize geometry
geo_disc = geo.discretize(npoints=npoints, method=sampler_method)

# the real_cord need to be added in geo_disc
geo_disc.user = GetRealPhyInfo(start_time, need_info='cord')

# N-S equation
pde = psci.pde.NavierStokes(
    nu=0.01,
    rho=1.0,
    dim=3,
    time_dependent=True,
    weight=[0.01, 0.01, 0.01, 0.01])

pde.set_time_interval([100.0, 110.0])

# boundary condition on left side: u=10, v=w=0
bc_left_u = psci.bc.Dirichlet('u', rhs=1.0, weight=1.0)
bc_left_v = psci.bc.Dirichlet('v', rhs=0.0, weight=1.0)
bc_left_w = psci.bc.Dirichlet('w', rhs=0.0, weight=1.0)

# boundary condition on right side: p=0
bc_right_p = psci.bc.Dirichlet('p', rhs=0.0, weight=1.0)

# boundary on circle
bc_circle_u = psci.bc.Dirichlet('u', rhs=0.0, weight=1.0)
bc_circle_v = psci.bc.Dirichlet('v', rhs=0.0, weight=1.0)
bc_circle_w = psci.bc.Dirichlet('w', rhs=0.0, weight=1.0)

# add bounday and boundary condition
pde.add_bc("left", bc_left_u, bc_left_v, bc_left_w)
pde.add_bc("right", bc_right_p)
pde.add_bc("circle", bc_circle_u, bc_circle_v, bc_circle_w)

# pde discretization 
pde_disc = pde.discretize(
    time_method="implicit", time_step=1, geo_disc=geo_disc)

# declare network
net = psci.network.FCNet(
    num_ins=3,
    num_outs=4,
    num_layers=num_layers,
    hidden_size=hidden_size,
    activation=activation)

# Algorithm
algo = psci.algorithm.PINNs(net=net, loss=None)

# Get data shape
npoints = len(pde_disc.geometry.interior)
data_size = len(geo_disc.user)

# create inputs/labels and its attributes
inputs, inputs_attr = algo.create_inputs(pde_disc)
labels, labels_attr = algo.create_labels(pde_disc)

main_program = paddle.static.Program()
startup_program = paddle.static.Program()

with paddle.static.program_guard(main_program, startup_program):
    # build and apply network
    algo.net.make_network()
    inputs_var = create_inputs_var(inputs)
    labels_var = create_labels_var(labels, npoints, data_size)
    outputs_var = [algo.net.nn_func(var) for var in inputs_var]

    # bc loss
    bc_loss = compute_bc_loss(inputs_attr, labels_attr, outputs_var, pde_disc)

    # eq loss
    output_var_0_eq_loss = compute_eq_loss(inputs_var[0], outputs_var[0],
                                           labels_var[0:3])

    output_var_4_eq_loss = compute_eq_loss(inputs_var[4], outputs_var[4],
                                           labels_var[7:10])
    # data_loss
    data_loss = l2_norm_square(outputs_var[4][:, 0]-labels_var[3]) + \
                l2_norm_square(outputs_var[4][:, 1]-labels_var[4]) + \
                l2_norm_square(outputs_var[4][:, 2]-labels_var[5]) + \
                l2_norm_square(outputs_var[4][:, 3]-labels_var[6])

    # total_loss
    total_loss = paddle.sqrt(bc_loss + output_var_0_eq_loss +
                             output_var_4_eq_loss + 100.0 * data_loss)
    opt_ops, param_grads = paddle.optimizer.Adam(learning_rate).minimize(
        total_loss)

# data parallel
nranks = paddle.distributed.get_world_size()
if nranks > 1:
    main_program, startup_program = convert_to_distributed_program(
        main_program, startup_program, param_grads)

inputs_name = [var.name for var in inputs_var]
inputs = data_parallel_partition(inputs)
feeds = dict(zip(inputs_name, inputs))

fetches = [total_loss.name] + [var.name for var in outputs_var]

# convert prim op to original if cinn is not enabled
if not psci.config.cinn_enabled():
    with paddle.static.program_guard(main_program, startup_program):
        if psci.config.prim_enabled():
            psci.config.prim2orig(main_program.block(0))
else:
    pass_manager = PassManager(
        [new_pass("build_cinn", {'feed': feeds,
                                 'fetch_list': fetches})])
    pass_manager.apply([main_program], [startup_program])

gpu_id = int(os.environ.get('FLAGS_selected_gpus', 0))
place = paddle.CUDAPlace(gpu_id)
exe = paddle.static.Executor(place)
exe.run(startup_program)

# num_epoch in train
train_epoch = epochs

# Solver time: (100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110]
num_time_step = 10
current_interior = np.zeros(
    (len(pde_disc.geometry.interior), 3)).astype(np.float32)
current_user = GetRealPhyInfo(start_time, need_info='physic')[:, 0:3]

build_flag = True
for i in range(num_time_step):
    next_time = start_time + (i + 1) * time_step
    print("############# train next time=%f train task ############" %
          next_time)
    self_lables = algo.feed_data_interior_cur(labels, labels_attr,
                                              current_interior)
    self_lables = algo.feed_data_user_cur(self_lables, labels_attr,
                                          current_user)
    self_lables = algo.feed_data_user_next(
        self_lables,
        labels_attr,
        GetRealPhyInfo(
            next_time, need_info='physic'))
    self_lables = data_parallel_partition(self_lables, time_step=i)

    for j in range(len(self_lables)):
        feeds['label' + str(j)] = self_lables[j]

    st = time.time()
    for k in range(train_epoch):
        out = exe.run(main_program, feed=feeds, fetch_list=fetches)
        print("autograd epoch: " + str(k + 1), "    loss:", out[0])
        if build_flag:
            build_flag = False
            print(f"Build cost {time.time()-st}")
            st = time.time()
    print(f"Step {i} loop run {time.time()-st}")
    next_uvwp = out[1:]

    # Save vtk
    file_path = vtk_filename + str(next_time)
    psci.visu.save_vtk(
        filename=file_path, geo_disc=pde_disc.geometry, data=next_uvwp)
    psci.visu.save_npy(
        filename=solution_filename + str(next_time),
        geo_disc=pde_disc.geometry,
        data=next_uvwp)

    # next_info -> current_info
    next_interior = np.array(next_uvwp[0])
    next_user = np.array(next_uvwp[-1])
    current_interior = next_interior[:, 0:3]
    current_user = next_user[:, 0:3]
