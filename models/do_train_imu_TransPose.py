import os
import pickle
import random
import time
from collections import defaultdict

import numpy as np
import torch
import wandb
from torch import optim
from tqdm import tqdm
from datetime import datetime

from utils.utils import tensor2numpy
from models.TransPose_net import TransPoseNet  # 导入TransPose网络
from torch.cuda.amp import autocast, GradScaler


def do_train_imu_TransPose(cfg, train_loader, test_loader=None, trial=None, model=None, optimizer=None):
    """
    训练IMU到全身姿态及物体变换的TransPose模型
    
    Args:
        cfg: 配置信息
        train_loader: 训练数据加载器
        test_loader: 测试数据加载器
        trial: Optuna试验（如果使用超参数搜索）
        model: 预训练模型（如果有）
        optimizer: 预训练模型的优化器（如果有）
    """
    # 初始化配置
    device = torch.device(f'cuda:{cfg.gpus[0]}' if torch.cuda.is_available() else 'cpu')
    model_name = cfg.model_name
    use_wandb = cfg.use_wandb
    pose_rep = 'rot6d'  # 使用6D表示
    max_epoch = cfg.epoch
    save_dir = cfg.save_dir
    scaler = GradScaler()

    # 打印训练配置
    print(f'Training: {model_name} (using TransPose), pose_rep: {pose_rep}')
    print(f'use_wandb: {use_wandb}, device: {device}')
    print(f'max_epoch: {max_epoch}')

    os.makedirs(save_dir, exist_ok=True)

    # 初始化模型（如果没有提供预训练模型）
    if model is None:
        model = TransPoseNet(cfg)
        print(f'Initialized TransPose model with {sum(p.numel() for p in model.parameters())} parameters')
        model = model.to(device)

        # 设置优化器（如果没有提供预训练优化器）
        if optimizer is None:
            optimizer = optim.AdamW(model.parameters(), lr=cfg.lr, weight_decay=cfg.weight_decay)
    else:
        print(f'Using pre-trained TransPose model with {sum(p.numel() for p in model.parameters())} parameters')
        model = model.to(device)
        
        # 如果没有提供优化器，创建新的优化器
        if optimizer is None:
            optimizer = optim.AdamW(model.parameters(), lr=cfg.lr, weight_decay=cfg.weight_decay)

    # 定义学习率调度器
    scheduler = optim.lr_scheduler.MultiStepLR(optimizer, milestones=cfg.milestones, gamma=cfg.gamma)

    # 如果使用wandb，初始化
    if use_wandb:
        wandb_project = cfg.wandb_project_name
        wandb.init(project=wandb_project, name=datetime.now().strftime("%m%d%H%M"), config=cfg)

    # 训练循环
    best_loss = float('inf')
    train_losses = defaultdict(list)
    test_losses = defaultdict(list)
    n_iter = 0

    for epoch in range(max_epoch):
        # 训练阶段
        model.train()
        train_loss = 0
        train_loss_rot = 0
        train_loss_root_pos = 0
        # train_loss_obj_trans = 0  # 已注释：模型不再预测物体平移
        train_loss_obj_rot = 0
        
        train_iter = tqdm(train_loader, desc=f'Epoch {epoch}')
        
        for batch in train_iter:
            # 准备数据 (确保所有需要的键都存在于batch中)
            root_pos = batch["root_pos"].to(device)  # [bs, seq, 3]
            motion = batch["motion"].to(device)  # [bs, seq, num_joints * joint_dim]
            human_imu = batch["human_imu"].to(device)  # [bs, seq, num_imus, 9]
            
            # 处理可选的物体数据
            bs, seq = human_imu.shape[:2]
            obj_imu = batch.get("obj_imu", None)
            obj_rot = batch.get("obj_rot", None)
            obj_trans = batch.get("obj_trans", None) # 新增：提取物体平移
            
            if obj_imu is not None:
                obj_imu = obj_imu.to(device)
            else:
                obj_imu = torch.zeros((bs, seq, 1, cfg.imu_dim if hasattr(cfg, 'imu_dim') else 9), device=device, dtype=human_imu.dtype)
                
            if obj_rot is not None:
                obj_rot = obj_rot.to(device)
            else:
                obj_rot = torch.zeros((bs, seq, 6), device=device, dtype=motion.dtype)

            if obj_trans is not None:
                obj_trans = obj_trans.to(device)
            else:
                obj_trans = torch.zeros((bs, seq, 3), device=device, dtype=root_pos.dtype)

            # 处理可选的BPS特征
            bps_features = batch.get("bps_features", None)
            if bps_features is not None:
                bps_features = bps_features.to(device)
            
            # 构建传递给模型的data_dict (包含所有需要的键)
            data_dict = {
                "human_imu": human_imu,
                "obj_imu": obj_imu,
                "motion": motion,             # 新增
                "root_pos": root_pos,           # 新增
                "obj_rot": obj_rot,             # 新增
                "obj_trans": obj_trans          # 新增
            }
            
            if bps_features is not None: # 如果有BPS特征，也加入
                data_dict["bps_features"] = bps_features
            
            # 前向传播
            optimizer.zero_grad()
            
            # TransPose直接输出预测结果
            pred_dict = model(data_dict)
            
            # 计算损失
            # 1. 姿态损失
            loss_rot = torch.nn.functional.mse_loss(pred_dict["motion"], motion)
            
            # 2. 根节点位置损失
            loss_root_pos = torch.nn.functional.mse_loss(pred_dict["root_pos"], root_pos)
            
            # # 3. 物体位置损失  # 已注释：模型不再预测物体平移
            # loss_obj_trans = torch.nn.functional.mse_loss(pred_dict["obj_trans"], obj_trans)
            
            # 4. 物体旋转损失
            loss_obj_rot = torch.nn.functional.mse_loss(pred_dict["obj_rot"], obj_rot)
            
            # 计算总损失（加权）
            if hasattr(cfg, 'loss_weights'):
                w_rot = cfg.loss_weights.rot if hasattr(cfg.loss_weights, 'rot') else 1.0
                w_root_pos = cfg.loss_weights.root_pos if hasattr(cfg.loss_weights, 'root_pos') else 0.1
                # w_obj_trans = cfg.loss_weights.obj_trans if hasattr(cfg.loss_weights, 'obj_trans') else 0.1  # 已注释：不需要这个权重
                w_obj_rot = cfg.loss_weights.obj_rot if hasattr(cfg.loss_weights, 'obj_rot') else 0.1
            else:
                # 默认权重
                w_rot = 1.0
                w_root_pos = 0.1
                # w_obj_trans = 0.1  # 已注释：不需要这个权重
                w_obj_rot = 1
            
            loss = w_rot * loss_rot + w_root_pos * loss_root_pos + w_obj_rot * loss_obj_rot  
            
            # 反向传播和优化
            scaler.scale(loss).backward()
            scaler.step(optimizer)
            scaler.update() 
            
            # 记录损失
            train_loss += loss.item()
            train_loss_rot += loss_rot.item()
            train_loss_root_pos += loss_root_pos.item()
            # train_loss_obj_trans += loss_obj_trans.item()  # 已注释：不需要累加这个损失
            train_loss_obj_rot += loss_obj_rot.item()
            
            # 更新tqdm描述
            train_iter.set_postfix({
                'loss': loss.item(),
                'rot': loss_rot.item(), 
                'root_pos': loss_root_pos.item(), 
                'obj_rot': loss_obj_rot.item()  # 更新：只显示物体旋转损失
            })
            
            # 记录wandb
            if use_wandb:
                log_dict = {
                    'train_loss': loss.item(),
                    'train_loss_rot': loss_rot.item(),
                    'train_loss_root_pos': loss_root_pos.item(),
                    'train_loss_obj_rot': loss_obj_rot.item()
                }
                wandb.log(log_dict, step=n_iter)
                
            n_iter += 1

        # 计算平均训练损失
        train_loss /= len(train_loader)
        train_loss_rot /= len(train_loader)
        train_loss_root_pos /= len(train_loader)
        train_loss_obj_rot /= len(train_loader)
        
        train_losses['loss'].append(train_loss)
        train_losses['loss_rot'].append(train_loss_rot)
        train_losses['loss_root_pos'].append(train_loss_root_pos)
        train_losses['loss_obj_rot'].append(train_loss_obj_rot)
        
        print(f'Epoch {epoch}, Train Loss: {train_loss:.6f}, '
              f'Rot Loss: {train_loss_rot:.6f}, '
              f'Root Pos Loss: {train_loss_root_pos:.6f}, '
              f'Obj Rot Loss: {train_loss_obj_rot:.6f}')

        # 每5个epoch进行一次测试和保存
        if epoch % 5 == 0 and test_loader is not None:
            # 测试阶段
            model.eval()
            test_loss = 0
            test_loss_rot = 0
            test_loss_root_pos = 0
            test_loss_obj_rot = 0
            
            with torch.no_grad():
                test_iter = tqdm(test_loader, desc=f'Test Epoch {epoch}')
                for batch in test_iter:
                    # 准备数据
                    root_pos = batch["root_pos"].to(device)
                    motion = batch["motion"].to(device)
                    human_imu = batch["human_imu"].to(device)
                    
                    bs, seq = human_imu.shape[:2]
                    obj_imu = batch.get("obj_imu", None)
                    obj_rot = batch.get("obj_rot", None)
                    obj_trans = batch.get("obj_trans", None) # 新增

                    if obj_imu is not None: obj_imu = obj_imu.to(device)
                    else: obj_imu = torch.zeros((bs, seq, 1, cfg.imu_dim if hasattr(cfg, 'imu_dim') else 9), device=device, dtype=human_imu.dtype)
                    
                    if obj_rot is not None: obj_rot = obj_rot.to(device)
                    else: obj_rot = torch.zeros((bs, seq, 6), device=device, dtype=motion.dtype)
                    
                    if obj_trans is not None: obj_trans = obj_trans.to(device)
                    else: obj_trans = torch.zeros((bs, seq, 3), device=device, dtype=root_pos.dtype)

                    bps_features = batch.get("bps_features", None)
                    if bps_features is not None: bps_features = bps_features.to(device)
                    
                    # 构建 data_dict_eval
                    data_dict_eval = {
                        "human_imu": human_imu,
                        "obj_imu": obj_imu,
                        "motion": motion,             # 新增
                        "root_pos": root_pos,           # 新增
                        "obj_rot": obj_rot,             # 新增
                        "obj_trans": obj_trans          # 新增
                    }
                    
                    if bps_features is not None:
                        data_dict_eval["bps_features"] = bps_features
                    
                    # TransPose直接输出预测结果
                    pred_dict = model(data_dict_eval)
                    
                    # 计算评估指标
                    # 1. 姿态损失
                    loss_rot = torch.nn.functional.mse_loss(pred_dict["motion"], motion)
                    
                    # 2. 根节点位置损失
                    loss_root_pos = torch.nn.functional.mse_loss(pred_dict["root_pos"], root_pos)
                    
                    # # 3. 物体位置损失  # 已注释：不计算这个损失
                    # loss_obj_trans = torch.nn.functional.mse_loss(pred_dict["obj_trans"], obj_trans)
                    
                    # 4. 物体旋转损失
                    loss_obj_rot = torch.nn.functional.mse_loss(pred_dict["obj_rot"], obj_rot)
                    
                    # 计算总损失（加权）
                    if hasattr(cfg, 'loss_weights'):
                        w_rot = cfg.loss_weights.rot if hasattr(cfg.loss_weights, 'rot') else 1.0
                        w_root_pos = cfg.loss_weights.root_pos if hasattr(cfg.loss_weights, 'root_pos') else 1.0  # 已注释：不需要这个权重
                        # w_obj_trans = cfg.loss_weights.obj_trans if hasattr(cfg.loss_weights, 'obj_trans') else 1.0  # 已注释：不需要这个权重
                        w_obj_rot = cfg.loss_weights.obj_rot if hasattr(cfg.loss_weights, 'obj_rot') else 1.0
                    else:
                        w_rot = 1.0
                        w_root_pos = 1.0
                        # w_obj_trans = 1.0  # 已注释：不需要这个权重
                        w_obj_rot = 1.0
                    
                    test_metric = w_rot * loss_rot + w_root_pos * loss_root_pos + w_obj_rot * loss_obj_rot
                    
                    # 记录损失
                    test_loss += test_metric.item()
                    test_loss_rot += loss_rot.item()
                    test_loss_root_pos += loss_root_pos.item()
                    test_loss_obj_rot += loss_obj_rot.item()
                    
                    # 更新tqdm描述
                    test_iter.set_postfix({
                        'test_metric': test_metric.item(),
                        'loss_rot': loss_rot.item(),
                        'loss_root_pos': loss_root_pos.item(),
                        'loss_obj_rot': loss_obj_rot.item()  # 更新：只显示物体旋转损失
                    })
            
            # 计算平均测试损失
            test_loss /= len(test_loader)
            test_loss_rot /= len(test_loader)
            test_loss_root_pos /= len(test_loader)
            test_loss_obj_rot /= len(test_loader)
            
            test_losses['loss'].append(test_loss)
            test_losses['loss_rot'].append(test_loss_rot)
            test_losses['loss_root_pos'].append(test_loss_root_pos)
            test_losses['loss_obj_rot'].append(test_loss_obj_rot)
            
            print(f'Epoch {epoch}, Test Metric: {test_loss:.6f}, '
                  f'Rot Loss: {test_loss_rot:.6f}, '
                  f'Root Pos Loss: {test_loss_root_pos:.6f}, '
                  f'Obj Rot Loss: {test_loss_obj_rot:.6f}')    
            
            if use_wandb:
                log_dict = {
                    'test_metric': test_loss,
                    'test_loss_rot': test_loss_rot,
                    'test_loss_root_pos': test_loss_root_pos,
                    'test_loss_obj_rot': test_loss_obj_rot
                }
                wandb.log(log_dict, step=n_iter)
            
            # 保存最佳模型
            if test_loss < best_loss:
                best_loss = test_loss
                save_path = os.path.join(save_dir, f'epoch_{epoch}_best.pt')
                print(f'Saving best model to {save_path}')
                torch.save({
                    'epoch': epoch,
                    'model_state_dict': model.state_dict(),
                    'optimizer_state_dict': optimizer.state_dict(),
                    'loss': best_loss,
                }, save_path)
                
        
        # 定期保存模型
        if epoch % 10 == 0:
            save_path = os.path.join(save_dir, f'epoch_{epoch}.pt')
            print(f'Saving model to {save_path}')
            torch.save({
                'epoch': epoch,
                'model_state_dict': model.state_dict(),
                'optimizer_state_dict': optimizer.state_dict(),
                'loss': train_loss,
            }, save_path)

        # 更新学习率
        scheduler.step()
    
    # 保存最终模型
    final_path = os.path.join(save_dir, 'final.pt')
    print(f'Saving final model to {final_path}')
    torch.save({
        'epoch': max_epoch - 1,
        'model_state_dict': model.state_dict(),
        'optimizer_state_dict': optimizer.state_dict(),
        'loss': train_loss,
    }, final_path)
    
    # 保存最终的损失曲线
    loss_curves = {
        'train_losses': train_losses,
        'test_losses': test_losses,
    }
    with open(os.path.join(save_dir, 'loss_curves.pkl'), 'wb') as f:
        pickle.dump(loss_curves, f)
    
    # 如果使用wandb，保存训练曲线
    if use_wandb:
        wandb.log({
            'final_train_loss': train_loss,
            'final_test_loss': test_loss if test_loader is not None else None,
            'best_test_loss': best_loss if test_loader is not None else None,
        })
        wandb.finish()
    
    # 如果是超参数搜索，返回最佳测试损失
    if trial is not None:
        return best_loss
        
    return model, optimizer


def load_transpose_model(cfg, checkpoint_path):
    """
    加载TransPose模型
    
    Args:
        cfg: 配置信息
        checkpoint_path: 模型检查点路径
        
    Returns:
        model: 加载的模型
    """
    device = torch.device(f'cuda:{cfg.gpus[0]}' if torch.cuda.is_available() else 'cpu')
    model = TransPoseNet(cfg).to(device)
    
    checkpoint = torch.load(checkpoint_path, map_location=device)
    model.load_state_dict(checkpoint['model_state_dict'])
    model.eval()
    
    print(f'Loaded TransPose model from {checkpoint_path}, epoch {checkpoint["epoch"]}')
    
    return model