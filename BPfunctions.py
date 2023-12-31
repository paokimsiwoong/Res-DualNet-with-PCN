import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import math
import copy

import json

# n01443537 같이 되어있는 클래스 이름들을 goldfish 와 같이 쉽게 바꿔줄 때 사용할 파일이 JSON파일
import os

# os.path.join(save_path, filename) 으로 파일 경로 합칠 때 사용
import shutil

# shutil.copyfile(path_a, path_b) a 경로의 파일을 b 경로에 복사

import time
import datetime

# 시간 측정에 사용


USE_CUDA = torch.cuda.is_available()
# GPU 사용가능하면 True 반환

device = torch.device("cuda" if USE_CUDA else "cpu")


# 훈련 weight 등 저장 함수
def save_state(state, is_best, save_path, filename, timestamp=""):
    filename = os.path.join(save_path, filename)
    torch.save(state, filename)

    if is_best:
        bestname = os.path.join(save_path, f"BP_model_best_{timestamp}.pth")
        shutil.copyfile(filename, bestname)
        # filename 경로에 있는 파일을 bestname 경로에 복사 저장


# 훈련 weight 등 불러오기 함수
def load_state(model: nn.Module, opt, lr_scheduler, load_path):
    load_dict = torch.load(load_path)

    model.load_state_dict(load_dict["model_state_dict"])
    opt.load_state_dict(load_dict["optimizer_state_dict"])
    lr_scheduler.load_state_dict(load_dict["lr_scheduler_state_dict"])


# loss history, acc history 저장 함수
def save_history(loss_history, acc_history, time_history, total_num_epochs, save_path, filename):
    filepath = os.path.join(save_path, filename)
    with open(filepath, "w") as f:
        json.dump([loss_history, acc_history, time_history, total_num_epochs], f)


def load_history(load_path):
    with open(load_path, "r") as f:
        contents = f.read()
        loss_history, acc_history, time_history, total_num_epochs = json.loads(contents)

    return loss_history, acc_history, time_history, total_num_epochs


# 현재 lr값을 알려주는 함수 -> 훈련 중 print에 사용
def get_lr(opt):
    for param_group in opt.param_groups:
        return param_group["lr"]


def accuracy(output, target, topk=(1,)):
    """Computes the accuracy over the k top predictions for the specified values of k"""
    with torch.no_grad():
        maxk = max(topk)
        batch_size = target.size(0)

        _, pred = output.topk(k=maxk, dim=1, largest=True, sorted=True)
        # pred는 [batch_size, maxk] 꼴
        # largest=True 이므로 각 행 1열이 최댓값 top1의 index이다.
        pred = pred.t()
        # transpose 해서 [maxk, batch_size] 로 변경
        correct = pred.eq(target.view(1, -1).expand_as(pred))
        # target을 view로 [1, batchsize] 꼴로 바꾸고
        # expand_as(pred) 로 [maxk, batch_size] 꼴이 되어 pred랑 동일한 size가 된다.
        # pred 가 각 열마다 top1, top2, top3 순으로 되어 있기때문에
        # correct는 각 열마다 top1 == target, top2 == target, top3 == target 순으로 되어 있음

        res = []
        respercent = []
        for k in topk:
            # topk = (1, maxk) 꼴
            correct_k = correct[:k].reshape(-1).float().sum(dim=0, keepdim=True)
            # k가 1일 때는 correct[:1]을 하면 [1, batch_size] 형태로 top1 == target 만 남고
            # k가 maxk 일때는 correct[:k] 는 [maxk, batch_size] 형태로 전부 다 들어 있음
            # 또 하나의 열은 전부 다 False 이거나 (top 1 ~ top k 다 틀림), 하나만 True (top 1 ~ top k 중 하나가 정답이랑 일치)
            # ==> 따라서 reshape(-1) 로 [maxk * batch_size] 꼴이 되어도 True 갯수는 batch_size를 넘지 않음
            # float() 로 변환한 후 sum함수로 다 더하면 정답 갯수가 나옴
            # keepdim=True를 두면 sum 값이 13.0 이렇게 float 하나만 반환하지 않고
            # [13.0] 과 같이 (1,) 꼴로 반환해줌

            percent_k = correct_k.mul(100.0 / batch_size)
            # .mul_을 쓰면 원본도 변경되므로 .mul

            correct_k = correct_k.detach().to("cpu").numpy()
            percent_k = percent_k.detach().to("cpu").numpy()
            # https://byeongjo-kim.tistory.com/32

            res.append(correct_k)
            respercent.append(percent_k)

        return res, respercent
        # res = [ndarray(top1 개수,), ndarray(top5 개수,)] 반환
        # respercent = [ndarray(top1 %,), ndarray(top5 %,)] 반환

        # res = [tensor([top1 개수]), tensor([topk 개수])] 반환
        # respercent = [tensor([top1 %]), tensor([topk %])] 반환


def loss_batch(criterion, output, target, opt=None, topk=(1,)):
    loss = criterion(output, target)
    count, acc = accuracy(output, target, topk)
    # acc = [ndarray(top1 %,), ndarray(top5 %,)] 이므로
    # acc1, acc5 = acc 를 하면
    # acc1 = ndarray(top1 %,)
    # acc5 = ndarray(top5 %,)
    # numpy array 안에 들어있다
    # ==> acc1[0] 과 acc5[0]을 해야 비로소 정확도 %값이 나옴

    # backprop 진행
    if opt is not None:  # 훈련이 아닐때는 opt=None이어서 역전파 수행 안함
        opt.zero_grad()
        loss.backward()
        opt.step()

    return loss.item(), count, acc


def loss_epoch(model, data_loader, criterion=None, sanity_check=False, opt=None):
    running_loss = 0.0
    running_top1_count = 0.0
    running_top5_count = 0.0
    len_data = len(data_loader.dataset)
    print(f"==>> len_data: {len_data}")
    # trainloader.dataset의 길이는 50000,
    # testloader.dataset은 10000

    # sanity check 때 사용
    # if sanity_check:
    #     temp = 0

    for x_b, y_b in data_loader:
        x_b = x_b.to(device)
        y_b = y_b.to(device)

        output = model(x_b)

        loss_b, (top1_count_b, top5_count_b), acc_b = loss_batch(
            criterion=criterion, output=output, target=y_b, opt=opt, topk=(1, 5)
        )
        # top1_count_b, top5_count_b = [ndarray(top1 개수,), ndarray(top5 개수,)]

        running_loss += loss_b

        running_top1_count += float(top1_count_b[0])
        # top1_count_b 는 크기가 1인 numpy array이므로 [0]으로 접근해야 값이 나옴
        running_top5_count += float(top5_count_b[0])

        if sanity_check:
            # temp += 1

            # if temp == 5:
            #     len_data = 160
            #     print(f"==>> len_data: {len_data}")
            #     break

            len_data = x_b.size(0)
            print(f"==>> len_data: {len_data}")
            break

            # 모델 설계 간단체크때는 첫배치만 하고 break해서
            # 매 epoch마다 똑같은 첫번째 배치만 사용하는 것 아닌가?
            # 지금 코드 방식으로는 dataloader가 iterator이기때문에
            # 매 epoch마다 전체를 도는 것은 아니지만
            # 배치 한개 하고나서 다음 epoch에서는 다른 배치를 사용
            # 또 dataloader가 shuffle도 하고 있기때문에 매번 dataloader를 초기화해도 문제?

    loss = running_loss / len_data

    acc1 = running_top1_count * 100.0 / len_data
    acc5 = running_top5_count * 100.0 / len_data

    return loss, acc1, acc5


def train_and_val(model, params):
    print(str(datetime.datetime.now()).split(".")[0])
    # 시작시간

    num_epochs = params["num_epochs"]
    t_loader = params["train_loader"]
    v_loader = params["val_loader"]

    criterion = params["criterion"]
    opt = params["optimizer"]
    lr_scheduler = params["lr_scheduler"]

    sanity_check = params["sanity_check"]

    save_path = params["save_path"]
    loss_history = params["loss_history"]
    acc_history = params["acc_history"]
    time_history = params["time_history"]
    total_num_epochs = params["total_num_epochs"]

    # save, load를 위해 수정할 때는 함수 인수로 load된 loss_history와 acc_history를 받기
    # loss_history = {'train':[], 'val':[]}
    # acc_history = {'train_top1':[], 'val_top1':[], 'train_top5':[], 'val_top5':[]}

    best_loss = float("inf")
    # float('inf') 는 +∞, float('-inf') 는 -∞ (@@int('inf')는 안된다)

    for epoch in range(num_epochs):
        start_time = time.time()

        current_lr = get_lr(opt)
        # print에 쓸 현재 learning rate 값 불러오기

        print("".center(50, "-"))
        print(f"Epoch {epoch}/{num_epochs-1}, current lr = {current_lr}")

        # 훈련
        model.train()

        train_loss, train_acc1, train_acc5 = loss_epoch(
            model=model, criterion=criterion, data_loader=t_loader, sanity_check=sanity_check, opt=opt
        )
        # loss_epoch 함수에서 이번 epoch forward pass backward pass 둘다 진행하고 loss값, acc값 다 계산

        loss_history["train"].append(train_loss)
        acc_history["train_top1"].append(train_acc1)
        acc_history["train_top5"].append(train_acc5)

        train_time = time.time()
        train_elapsed_time = datetime.timedelta(seconds=(train_time - start_time))
        train_elapsed_time_ = str(train_elapsed_time).split(".")[0]

        print(
            f"train loss: {train_loss:>.9}, train accuracy: (top1: {train_acc1:3.2f}%, top5: {train_acc5:3.2f}%)"
        )
        print(f"elapsed time: {train_elapsed_time_}")

        # 검증
        model.eval()

        with torch.no_grad():
            val_loss, val_acc1, val_acc5 = loss_epoch(
                model=model, criterion=criterion, data_loader=v_loader, sanity_check=sanity_check
            )

        loss_history["val"].append(val_loss)
        acc_history["val_top1"].append(val_acc1)
        acc_history["val_top5"].append(val_acc5)

        is_best = False
        if val_loss < best_loss:
            best_loss = val_loss
            is_best = True

        lr_scheduler.step(val_loss)
        # lr 값을 변경할지 말지 lr_scheduler가 판단하도록 val_loss 넘기기

        val_time = time.time()
        val_elapsed_time = datetime.timedelta(seconds=(val_time - train_time))
        val_elapsed_time_ = str(val_elapsed_time).split(".")[0]

        print(f"val loss: {val_loss:>.9}, val accuracy: (top1: {val_acc1:3.2f}%, top5: {val_acc5:3.2f}%)")
        print(f"elapsed time: {val_elapsed_time_}")

        epoch_elapsed_time = train_elapsed_time + val_elapsed_time
        epoch_elapsed_time_ = str(epoch_elapsed_time).split(".")[0]
        print(f" epoch elapsed time = {epoch_elapsed_time_}")
        time_history.append(epoch_elapsed_time_)

        # @@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
        # 이번 epoch weight, opt, lr_scheduler 저장
        # timestamp = str(datetime.datetime.now()).split(" ")[0]
        timestamp = str(datetime.datetime.now()).split(" ")
        timestamp = timestamp[0] + "-" + ";".join(timestamp[1].split(".")[0].split(":"))
        # 2023-08-08 또는 2023-08-08_13;52;06
        # @@@@@@@ 파일 이름에 : 사용 불가능 ==> ; 로 변경

        if sanity_check is False:
            save_state(
                state={
                    "model_state_dict": model.state_dict(),
                    "optimizer_state_dict": opt.state_dict(),
                    "lr_scheduler_state_dict": lr_scheduler.state_dict(),
                },
                is_best=is_best,
                save_path=save_path,
                filename=f"BP_{timestamp}_epoch_{total_num_epochs}.pth",
                timestamp=timestamp,
            )

        # @@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@

        total_num_epochs += 1
        print(f"==>> total_num_epochs: {total_num_epochs}")
        save_history(
            loss_history=loss_history,
            acc_history=acc_history,
            time_history=time_history,
            total_num_epochs=total_num_epochs,
            save_path=save_path,
            filename=f"BP_{timestamp}_history.json",
        )

    return model, loss_history, acc_history, time_history, total_num_epochs
