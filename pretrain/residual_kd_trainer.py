import os
import json
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.distributed as dist
from torch.distributed import get_rank
from tqdm import tqdm
from torch.utils.data import DataLoader, DistributedSampler
from utils import print_rank, get_model, save_rank, save_parallel, get_tokenizer, all_gather

from .trainer import PreTrainer


class ResidualKDPreTrainer(PreTrainer):
    def __init__(self, args, ds_config, device, do_train=True):
        super().__init__(args, ds_config, device, do_train)
        self.setup_teacher_model()
        self.setup_base_model()
        
    def setup_teacher_model(self, args=None, device=None):
        args = args or self.args
        device = device or self.device
        assert args.teacher_model_path is not None
        teacher_model = get_model(args, device, args.teacher_model_path, from_scratch=False)
        teacher_model.eval()
        self.teacher_model = teacher_model

    def setup_base_model(self, args=None, device=None):
        args = args or self.args
        device = device or self.device
        assert args.base_model_path is not None
        base_model = get_model(args, device, args.base_model_path, from_scratch=False)
        base_model.eval()
        self.base_model = base_model

    def _get_kd_loss(self, logits, teacher_logits, loss_mask):
        teacher_probs = F.softmax(teacher_logits, dim=-1, dtype=torch.float32)
        inf_mask = torch.isinf(logits)
        logprobs = F.log_softmax(logits, dim=-1, dtype=torch.float32)
        prod_probs = torch.masked_fill(teacher_probs * logprobs, inf_mask, 0)
        x = torch.sum(prod_probs, dim=-1)
        kd_loss = -torch.sum(x * loss_mask, dim=-1) / torch.sum(loss_mask, dim=-1)
        return kd_loss

    def _compute_kd_lm_loss(self, model_batch, no_model_batch, mean=True, output_all_losses=False):
        logits = self.model(**model_batch, use_cache=False).logits
        with torch.no_grad():
            teacher_logits = self.teacher_model(**model_batch, use_cache=False).logits
            base_logits = self.base_model(**model_batch, use_cache=False).logits
        
        total_logits = logits + self.args.rsd_mix_ratio * base_logits
        
        # lm loss
        lm_loss = self._get_lm_loss_from_logits(total_logits, no_model_batch["label"], no_model_batch["loss_mask"])
        
        # probs = torch.softmax(total_logits, dim=-1)
        # one_hot_labels = F.one_hot(no_model_batch["label"], num_classes=probs.shape[-1])
        
        # grad_on_logits = one_hot_labels - probs
        
        # tmp = grad_on_logits.norm(dim=-1) ** 0.5
        # tmp = tmp * no_model_batch["loss_mask"] / torch.sum(no_model_batch["loss_mask"], dim=-1, keepdim=True)
        # tmp = tmp * 2048 / 4
        # print_rank(tmp)
        
        
        # kd loss
        kd_loss = self._get_kd_loss(total_logits, teacher_logits, no_model_batch["loss_mask"])
        kd_entropy = self._get_kd_loss(teacher_logits, teacher_logits, no_model_batch["loss_mask"])
        
        # residual_real_loss
        residual_real_loss, residual_entropy = None, None
        if self.args.kd_rsd_loss is not None:
            residual_truth = teacher_logits - base_logits
            residual_real_loss = self._get_kd_loss(logits, residual_truth, no_model_batch["loss_mask"])
            residual_entropy = self._get_kd_loss(residual_truth, residual_truth, no_model_batch["loss_mask"])
        
        # loss
        loss = (1-self.args.kd_ratio) * lm_loss + self.args.kd_ratio * kd_loss
        if self.args.kd_rsd_loss is not None:
            loss += self.args.kd_rsd_loss * residual_real_loss
        
        if mean:
            loss = loss.mean()
            lm_loss = lm_loss.mean()
            kd_loss = kd_loss.mean()
            kd_entropy = kd_entropy.mean()
            if residual_real_loss is not None:
                residual_real_loss = residual_real_loss.mean()
                residual_entropy = residual_entropy.mean()
        
        outputs = {
            "loss": loss,
            "lm_loss": lm_loss,
            "kd_loss": kd_loss,
            "kd_entropy": kd_entropy,
            "residual_real_loss": residual_real_loss,
            "residual_entropy": residual_entropy,
        }
        
        if output_all_losses:
            teacher_loss = self._get_lm_loss_from_logits(teacher_logits, no_model_batch["label"], no_model_batch["loss_mask"])
            base_loss = self._get_lm_loss_from_logits(base_logits, no_model_batch["label"], no_model_batch["loss_mask"])
            residual_loss = self._get_lm_loss_from_logits(logits, no_model_batch["label"], no_model_batch["loss_mask"])

            if mean:
                teacher_loss = teacher_loss.mean()
                base_loss = base_loss.mean()
                residual_loss = residual_loss.mean()

            outputs.update({
                "teacher_loss": teacher_loss,
                "base_loss": base_loss,
                "residual_loss": residual_loss
            })

            if residual_real_loss is None:
                residual_truth = teacher_logits - base_logits
                residual_real_loss = self._get_kd_loss(logits, residual_truth, no_model_batch["loss_mask"])
                residual_entropy = self._get_kd_loss(residual_truth, residual_truth, no_model_batch["loss_mask"])
                if mean:
                    residual_real_loss = residual_real_loss.mean()
                    residual_entropy = residual_entropy.mean()
                
                outputs.update({
                    "residual_real_loss": residual_real_loss,
                    "residual_entropy": residual_entropy,
                })
            
        return outputs
    
    def compute_loss(self, model_batch, no_model_batch):
        out = self._compute_kd_lm_loss(model_batch, no_model_batch)
        loss, lm_loss, kd_loss, kd_entropy, residual_real_loss, residual_entropy = \
            out["loss"], out["lm_loss"], out["kd_loss"], out["kd_entropy"], out["residual_real_loss"], out["residual_entropy"]
        
        dist.all_reduce(lm_loss, group=self.dp_group, op=dist.ReduceOp.SUM)
        lm_loss = lm_loss / self.dp_world_size
        dist.all_reduce(kd_loss, group=self.dp_group, op=dist.ReduceOp.SUM)
        kd_loss = kd_loss / self.dp_world_size
        dist.all_reduce(kd_entropy, group=self.dp_group, op=dist.ReduceOp.SUM)
        kd_entropy = kd_entropy / self.dp_world_size
        other_outputs = {"lm_loss": lm_loss.item(), "kd_loss": kd_loss.item(), "kd_entropy": kd_entropy.item()}
        if residual_real_loss is not None:
            dist.all_reduce(residual_real_loss, group=self.dp_group, op=dist.ReduceOp.SUM)
            residual_real_loss = residual_real_loss / self.dp_world_size
            dist.all_reduce(residual_entropy, group=self.dp_group, op=dist.ReduceOp.SUM)
            residual_entropy = residual_entropy / self.dp_world_size
            other_outputs["residual_real_loss"] = residual_real_loss.item()
            other_outputs["residual_entropy"] = residual_entropy.item()
        
        return loss, other_outputs
    
    def evaluate(self):
        eval_sampler = DistributedSampler(self.eval_dataset, shuffle=False, drop_last=False, rank=self.dp_rank, num_replicas=self.dp_world_size)
        eval_dataloader = DataLoader(
            self.eval_dataset, sampler=eval_sampler, batch_size=self.args.eval_batch_size, num_workers=self.args.num_workers, collate_fn=self.eval_dataset.collate_lm)
        
        self.model.eval()
        all_losses, all_lm_losses, all_kd_losses, all_kd_entropy = [], [], [], []
        all_teacher_losses, all_base_losses, all_residual_losses, all_residual_real_losses = [], [], [], []
        all_residual_entropy = []
                    
        with torch.no_grad():
            for i, (model_batch, no_model_batch) in tqdm(enumerate(eval_dataloader), f"LM Evaluation", disable=(not get_rank() == 0)):
                if i % 10 == 0:
                    print_rank(f"evaluating batch {i}/{len(eval_dataloader)}")
                self.eval_dataset.move_to_device(model_batch, no_model_batch, self.device)
                out = self._compute_kd_lm_loss(
                    model_batch, no_model_batch, mean=False, output_all_losses=True)
                loss, lm_loss, kd_loss, kd_entropy, teacher_loss, base_loss, residual_loss, residual_real_loss, residual_entropy = \
                    out["loss"], out["lm_loss"], out["kd_loss"], out["kd_entropy"], out["teacher_loss"], out["base_loss"], out["residual_loss"], out["residual_real_loss"], out["residual_entropy"]
                all_losses.append(loss)
                all_lm_losses.append(lm_loss)
                all_kd_losses.append(kd_loss)
                all_kd_entropy.append(kd_entropy)
                all_teacher_losses.append(teacher_loss)
                all_base_losses.append(base_loss)
                all_residual_losses.append(residual_loss)
                all_residual_real_losses.append(residual_real_loss)
                all_residual_entropy.append(residual_entropy)
        
        all_losses = torch.cat(all_losses, dim=0)
        avg_loss = self._avg_loss_cross_dp(all_losses)

        all_lm_losses = torch.cat(all_lm_losses, dim=0)
        avg_lm_loss = self._avg_loss_cross_dp(all_lm_losses)
        
        all_kd_losses = torch.cat(all_kd_losses, dim=0)
        avg_kd_loss = self._avg_loss_cross_dp(all_kd_losses)
        
        all_kd_entropy = torch.cat(all_kd_entropy, dim=0)
        avg_kd_entropy = self._avg_loss_cross_dp(all_kd_entropy)
        
        all_teacher_losses = torch.cat(all_teacher_losses, dim=0)
        avg_teacher_loss = self._avg_loss_cross_dp(all_teacher_losses)
        
        all_base_losses = torch.cat(all_base_losses, dim=0)
        avg_base_loss = self._avg_loss_cross_dp(all_base_losses)
        
        all_residual_losses = torch.cat(all_residual_losses, dim=0)
        avg_residual_loss = self._avg_loss_cross_dp(all_residual_losses)
        
        all_residual_real_losses = torch.cat(all_residual_real_losses, dim=0)
        avg_residual_real_loss = self._avg_loss_cross_dp(all_residual_real_losses)
        
        all_residual_entropy = torch.cat(all_residual_entropy, dim=0)
        avg_residual_entropy = self._avg_loss_cross_dp(all_residual_entropy)

        if get_rank() == 0:
            res = {"avg_loss": avg_loss,
                   "avg_lm_loss": avg_lm_loss,
                   "avg_kd_loss": avg_kd_loss,
                   "avg_kd_entropy": avg_kd_entropy,
                   "avg_teacher_loss": avg_teacher_loss,
                   "avg_base_loss": avg_base_loss,
                   "avg_residual_loss": avg_residual_loss,
                   "avg_residual_real_loss": avg_residual_real_loss,
                   "avg_residual_entropy": avg_residual_entropy,}
            eval_log_str = self.get_log(res, "eval")
            print_rank(eval_log_str)
            save_rank(eval_log_str, os.path.join(self.args.save, "log.txt"))
            print_rank("*" * 100)
        else:
            res = None
        
        dist.barrier()
        
    def save(self, directory):
        super().save(directory)
        base_ckpt_path = directory or self.args.save
        ckpt_dir = os.path.join(base_ckpt_path, f"{self.global_steps}")
        if get_rank() == 0:
            with open(os.path.join(ckpt_dir, "base_model.json"), "w") as f:
                json.dump({
                    "base_model_path": self.args.base_model_path.replace(self.args.base_path, "").strip("/"),
                    "base_ckpt_name": self.args.base_ckpt_name,
                }, f)
        
        dist.barrier()