import torch
import logging
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import seaborn
from collections import OrderedDict
import torch.nn as nn
import torch.nn.functional as F
from model.utils import Metric
from time import time
from sklearn.cluster import KMeans
from sklearn.manifold import TSNE
from scipy.stats import pearsonr, spearmanr
from sklearn.metrics import accuracy_score
from sklearn.metrics.pairwise import paired_cosine_distances, paired_euclidean_distances, paired_manhattan_distances
from scipy.optimize import linear_sum_assignment
from scipy.stats import spearmanr, kendalltau
from sklearn.metrics.cluster import adjusted_rand_score, normalized_mutual_info_score
from dataclasses import dataclass, fields
from prettytable import PrettyTable

logger = logging.getLogger(__name__)

@dataclass
class EvaluationResult(OrderedDict):
    RI: float = 0.          # Adjusted Rand Index
    NMI: float = 0.         # Normalized Mutual Informatio
    acc: float = 0.         # Clustering Accuracy
    purity: float = 0.      # Clutering Purity
    SR: float = 0.          # Semantic Relatedness
    MRR: float = 0.         # 平均倒数排名（Mean Reciprocal Rank, MRR）
    MAP: float = 0.         # Mean Average Precision
    all_mean: float = 0.
    alignment: float = 0.
    adjusted_alignment: float = 0.
    uniformity: float = 0.

    def __post_init__(self):
        self.positive_metrics = ['RI', 'NMI', 'acc', 'purity', 'SR', 'MRR', 'MAP'] 
        self.negative_metrics = []
        self.not_metrics = ['all_mean', 'alignment', 'adjusted_alignment', 'uniformity']

        self.all_mean = 0.
        self.mean()
        class_fields = fields(self)
        for field in class_fields:
            v = getattr(self, field.name)
            if v is not None:
                self[field.name] = v

    def __lt__(self, other):
        return self.purity < other.purity

    def mean(self):
        all_values = []
        for key, value in self.__dict__.items():
            if key in self.positive_metrics:
                all_values.append(value)
            elif key in self.negative_metrics:
                all_values.append(-value)
            else:
                pass

        self.all_mean = sum(all_values) / (len(all_values))     # ignore the 'all_mean' when averaging
        return self.all_mean

    def update(self, new_result):
        """
        :param new_result: EvaluationResult
        :return:
        """
        self.RI = new_result.RI
        self.NMI = new_result.NMI
        self.acc = new_result.acc
        self.purity = new_result.purity
        self.SR = new_result.SR
        self.MRR = new_result.MRR
        self.MAP = new_result.MAP

    def show(self, logger=None, note=None):
        if logger is not None:
            logger.info("\nclustering_task [%s]: RI: %s NMI: %s Acc: %s Purity: %s" % (note, self.RI, self.NMI, self.acc, self.purity))
            logger.info("\nSemantic Relatedness [%s]: SR: %s" % (note, self.SR))
            logger.info("\nSession Retrieval [%s]: MRR: %s MAP: %s" % (note, self.MRR, self.MAP))
            logger.info("\nRepresentation_Evaluation [%s]: Alignment: %.6f Alignment (adjusted): %.6f Uniformity: %.6f" % (note, self.alignment, self.adjusted_alignment, self.uniformity))

            tb = PrettyTable()
            tb.field_names = ['', 'RI', 'NMI', 'Acc', 'Purity', 'SR', 'MRR', 'MAP', 'Alignment', 'Adjusted Alignment', 'Uniformity']
            tb.add_row(['Metrics'] + \
                       ['%.2f' % (v * 100) for v in [self.RI, self.NMI, self.acc, self.purity, self.SR, self.MRR, self.MAP]] + \
                       ['%.2f' % v for v in [self.alignment, self.adjusted_alignment, self.uniformity]])
            logger.info('\n' + tb.__str__())


def get_accuracy(y_true, y_pred):
    """
    计算聚类的准确率
    """
    y_true = y_true.astype(np.int64)

    assert y_pred.size == y_true.size

    D = max(y_pred.max(), y_true.max()) + 1
    w = np.zeros((D, D), dtype=np.int64)
    for i in range(y_pred.size):
        w[y_pred[i], y_true[i]] += 1
    ind = linear_sum_assignment(w.max() - w)
    ind = np.array(ind).T

    return sum([w[i, j] for i, j in ind]) * 1.0 / y_pred.size

def purity_score(y_true, y_pred):
    """Purity score
        Args:
            y_true(np.ndarray): n*1 matrix Ground truth labels
            y_pred(np.ndarray): n*1 matrix Predicted clusters

        Returns:
            float: Purity score

        Reference: https://blog.csdn.net/weixin_45727931/article/details/111921581
    """
    # matrix which will hold the majority-voted labels
    y_true = y_true.astype(np.int64)
    y_voted_labels = np.zeros(y_true.shape)
    # Ordering labels
    ## Labels might be missing e.g with set like 0,2 where 1 is missing
    ## First find the unique labels, then map the labels to an ordered set
    ## 0,2 should become 0,1
    labels = np.unique(y_true)
    ordered_labels = np.arange(labels.shape[0])
    for k in range(labels.shape[0]):
        y_true[y_true == labels[k]] = ordered_labels[k]
    # Update unique labels
    labels = np.unique(y_true)
    # We set the number of bins to be n_classes+2 so that
    # we count the actual occurence of classes between two consecutive bins
    # the bigger being excluded [bin_i, bin_i+1[
    bins = np.concatenate((labels, [np.max(labels) + 1]), axis=0)

    for cluster in np.unique(y_pred):
        hist, _ = np.histogram(y_true[y_pred == cluster], bins=bins)
        # Find the most present label in the cluster
        winner = np.argmax(hist)
        y_voted_labels[y_pred == cluster] = winner

    return accuracy_score(y_true, y_voted_labels)

def feature_cosine_matrix(features):
    """
    :param features: numpy.array [N, dim]
    :return:
    """
    dot_product = np.matmul(features, features.T)
    norm = np.linalg.norm(features, axis=-1).reshape(-1, 1)
    cosine_matrix = dot_product / (np.matmul(norm, norm.T) + 1e-6)
    return cosine_matrix

def precalculate_scores_from_subject_and_model(y_true, features): # 출력값 찍어보기 -> 중요
    """
    :param y_true:      ground_truth labels about domains
    :param features:    produced features
    :return:
    """
    #print("======= y_true: ", y_true)
    y_true = y_true.astype(int).reshape(-1, 1)
    M_y = np.repeat(y_true, repeats=len(y_true), axis=-1)
    #print("======= M_y: ", M_y)

    assert (M_y.shape[0] == M_y.shape[1])

    scores_from_subject = (M_y == M_y.T).astype(float) # .reshape(-1)
    #print("======= features: ", features)
    scores_from_model = feature_cosine_matrix(features) # .reshape(-1)
    #print("======= scores_from_subject: ", scores_from_subject)
    #print("======= scores_from_model: ", scores_from_model)

    return scores_from_subject, scores_from_model

def skip_diag_strided(A):
    """
    删除numpy的对角元
    ref: https://qa.1r1g.com/sf/ask/3271538091/
    :param A:   numpy.array [N, N]
    :return:
    """
    assert (A.shape[0] == A.shape[1])
    m = A.shape[0]
    strided = np.lib.stride_tricks.as_strided
    s0, s1 = A.strides
    return strided(A.ravel()[1:], shape=(m-1, m), strides=(s0+s1, s1)).reshape(m, -1)

def semantic_relatedness_precise(y_true=None, features=None, scores_from_subject=None, scores_from_model=None, dtype='float64'):
    """
    :param y_true:      ground_truth labels about domains
    :param features:    produced features
    :return:
    주요 목적은 모델이 예측한 특징과 주어진 실제 라벨 간의 관련성을 계산하는 것
    """
    if scores_from_subject is None and scores_from_model is None:
        y_true = y_true.astype(int).reshape(-1, 1)
        M_y = np.repeat(y_true, repeats=len(y_true), axis=-1)

        assert (M_y.shape[0] == M_y.shape[1])

        scores_from_subject = (M_y == M_y.T).astype(float) # .reshape(-1)
        scores_from_model = feature_cosine_matrix(features) # .reshape(-1)

        scores_from_subject = skip_diag_strided(scores_from_subject)
        scores_from_model = skip_diag_strided(scores_from_model)

    scores_from_subject = scores_from_subject.astype(dtype)
    scores_from_model = scores_from_model.astype(dtype)

    correlation = 0.
    for i in range(scores_from_subject.shape[0]):
        corr, p_value = spearmanr(scores_from_subject[i, :], scores_from_model[i, :])
        # corr, p_value = kendalltau(scores_from_subject[i, :], scores_from_model[i, :])
        if np.isnan(corr):  # there is a bug
            corr = 0.
        correlation += corr / scores_from_subject.shape[0]
    # print('========= correlation: ', correlation, correlation.shape)
    return correlation

def get_rankings(scores_from_model, dtype='float64'):
    """
    Get rankings of the ground truth samples.
    :param scores_from_subject:
    :param scores_from_model:
    :return:
    """
    rank1 = np.argsort(-scores_from_model.astype(dtype), axis=-1) # scores_from_model 배열에 대해 내림차순으로 정렬된 순위(rank)를 계산, 내림차순으로 정렬
    rank2 = np.argsort(rank1, axis=-1) # 정렬된 결과를 기준으로 순위(rank)를 다시 계산, 가장 높은 점수를 가진 샘플의 순위는 1이 되고, 그 다음으로 높은 점수를 가진 샘플의 순위는 2
    rankings = rank2 + 1
    return rankings


def mean_average_precision(scores_from_subject, all_rankings, dtype='float64'):
    map_score = 0.
    scores_from_subject = scores_from_subject.astype(dtype)

    n_samples = scores_from_subject.shape[0] # scores_from_subject의 행 수를 통해 샘플의 수를 가져옴
    for i in range(n_samples):
        #print('========== all_rankings:', all_rankings)
        rankings = np.sort(all_rankings[i][scores_from_subject[i] == 1]) # 이진 레이블이 1인 인덱스에 해당하는 순위를 정렬
        #print('========== rankings:', rankings)
        cumsum = np.cumsum(scores_from_subject[i][scores_from_subject[i] == 1]) # 누적 합계를 계산, 배열 [1, 2, 3, 4]의 경우 cumsum은 [1, 3, 6, 10]을 반환
        #print('========== cumsum:', cumsum)

        reciprocal_rankings = cumsum / rankings # 누적 합계를 해당 인덱스에 대한 순위로 나눔
        #print('========== reciprocal_rankings:', reciprocal_rankings)

        map_score += np.mean(reciprocal_rankings) / n_samples # 이러한 값의 평균을 구하고 map_score에 추가
        #print('========== map_score:', map_score)

    return map_score


def mean_reciprocal_rank(scores_from_subject, scores_from_model, dtype='float64'):
    """
    ref: https://gist.github.com/bwhite/3726239
    :param scores_from_subject:
    :param scores_from_model:
    :param dtype:
    :return:
    """
    sorting_index = np.argsort(scores_from_model.astype(dtype), axis=-1)
    sorted_scores_from_subject = np.take_along_axis(scores_from_subject.astype(dtype), sorting_index, axis=-1)[:, ::-1]
    rs = np.array([r.tolist().index(1) for r in sorted_scores_from_subject])
    return float(np.mean(1. / (rs + 1)))

def tsne_visualization(features, y_true, output_filename):
    y_true = y_true.astype(int)
    tsne = TSNE()
    tsne.fit_transform(features)
    tsne = pd.DataFrame(tsne.embedding_, index=y_true)

    n_class = np.max(y_true)

    color_candidates = list(seaborn.xkcd_rgb.values())
    # random.shuffle(color_candidates)
    for i in range(n_class):
        d = tsne[y_true == i]
        plt.scatter(d[0], d[1], color=color_candidates[i], marker='.')

    fig = plt.figure()
    plt.savefig(output_filename)
    plt.close()

def align_loss(x, y, label, alpha=2):
    """
    bsz : batch size (number of positive pairs)
    d   : latent dim
    x   : Tensor, shape=[bsz, d]
          latents for one side of positive pairs
    y   : Tensor, shape=[bsz, d]
          latents for the other side of positive pairs
    label: Tensor, shape=[bsz]
          whether (x,y) is a positive pair.

    ref: https://github.com/SsnL/align_uniform
    """

    # return (x - y).norm(p=2, dim=1).pow(alpha).mean()
    # return ((x - y).norm(p=2, dim=1).pow(alpha) * label).sum() / label.sum()  # TODO: 正例对数不同
    return ((x - y).norm(p=2, dim=1).pow(alpha) * label).sum(), label.sum()


def uniform_loss(x, t=2):
    """
    bsz : batch size (number of positive pairs)
    d   : latent dim
    x   : Tensor, shape=[bsz, d]
          latents for one side of positive pairs
    y   : Tensor, shape=[bsz, d]
          latents for the other side of positive pairs

    ref: https://github.com/SsnL/align_uniform
    """
    return torch.pdist(x, p=2).pow(2).mul(-t).exp().mean().log()

def align_uniform(normalized_features, labels=None, device='cpu'):
    """
    :param normalized_features: torch.tensor
    :param labels:              torch.tensor/numpy.array
    :return:
    """
    if device == 'cpu':
        labels = labels.astype(int).reshape(-1, 1)
        M_y = np.repeat(labels, repeats=len(labels), axis=-1)

        assert (M_y.shape[0] == M_y.shape[1])

        scores_from_subject = (M_y == M_y.T).astype(bool)  # .reshape(-1)
    else:
        labels = labels.view(-1, 1)
        M_y = labels.repeat(1, labels.shape[0])
        assert (M_y.shape[0] == M_y.shape[1])
        scores_from_subject = (M_y == M_y.T).int()
        scores_from_subject = scores_from_subject.to(device)

    positive_alignment, negative_alignment, uniformity = 0., 0., 0.
    n_positive, n_negative = 0, 0
    n_samples = scores_from_subject.shape[0]
    normalized_features = normalized_features.to(device)

    for i in range(n_samples):
        feat1, feat2 = normalized_features[i, :].view(1, -1).repeat(n_samples, 1), normalized_features
        pos_align, n_pos = align_loss(feat1,
                                      feat2,
                                      scores_from_subject[i, :],
                                      alpha=2)
        neg_align, n_neg = align_loss(feat1,
                                      feat2,
                                      1-scores_from_subject[i, :],
                                      alpha=2)

        positive_alignment += pos_align
        n_positive += n_pos
        negative_alignment += neg_align
        n_negative += n_neg

        uniformity += uniform_loss(torch.cat([feat1, feat2], dim=-1),
                                   t=2)

    positive_alignment /= n_positive
    negative_alignment /= n_negative

    return positive_alignment, positive_alignment - negative_alignment, uniformity / n_samples


class Loss():

    def __init__(self, args):
        self.args = args
        self.cos = nn.CosineSimilarity(dim=2)
        self.metric = Metric(args)

    def train_loss_fct(self, config, p, n): # Positive와 Negative 샘플에 대한 임베딩 간 유사도를 계산하고, Contrastive Loss를 계산
        
        loss = []
        
        for i in range(p.shape[0]): # p.shape[0]: batch_size
            
            pos_cos_sim = self.cos(p[i].unsqueeze(1), p[i].unsqueeze(0)) / self.args.temperature
            mask_pos = torch.eye(pos_cos_sim.size(0)).bool().to(self.args.device)
            pos_cos_sim.masked_fill_(mask_pos, 0) # 대각행렬을 0으로 채움
            # print("=======pos_cos_sim: ", pos_cos_sim)
            
            neg_cos_sim = self.cos(n[i].unsqueeze(1), n[i].unsqueeze(0)) / self.args.temperature
            mask_neg = torch.eye(neg_cos_sim.size(0)).bool().to(self.args.device)
            neg_cos_sim.masked_fill_(mask_neg, 0)
            # print("=======neg_cos_sim: ", neg_cos_sim)
            
            cosine_similarity = torch.cat([pos_cos_sim, neg_cos_sim], dim=1).to(self.args.device)
            labels = torch.arange(cosine_similarity.size(0)).long().to(self.args.device)
            
            log_softmax = nn.LogSoftmax(dim=1)
            log_probs = log_softmax(cosine_similarity)
            dial_loss = config['criterion'](log_probs, labels)  # criterion = nn.CrossEntropyLoss() 대신 criterion = nn.NLLLoss()

            # print("=======dial_loss: ", dial_loss)
            loss.append(dial_loss)
        
        #print("=======loss: ", torch.stack(loss).sum() / len(loss))

        return torch.stack(loss).sum() / len(loss)

    def evaluation_during_training(self, features, labels, gpu_features=None, n_average=1,
                                   tsne_visualization_output=None, tasks=None, dtype='float64', logger=None, note=''):
        
        labels = np.array(labels).astype(int)
        features = features.cpu().numpy().astype(dtype)
        if gpu_features is not None:
            gpu_labels = torch.tensor(labels, device='cpu')
        if features is not None:
            features = np.array(features).astype(dtype)
        
        # n_classes
        label_set = set()
        for s in labels:
            label_set.add(s)
        
        # initialize
        RI, NMI, acc, purity = 0., 0., 0., 0.
        clustering_time, RI_time, NMI_time, acc_time, purity_time = 0., 0., 0., 0., 0.
        SR, SR_time = 0., 0.
        MRR, MAP, mrr_time, map_time, ranking_time, scoring_time = 0., 0., 0., 0., 0., 0.
        alignment, adjusted_alignment, uniformity = 0., 0., 0.
        align_uniform_time = 0.       
        
        # KMeans
        if 'clustering' in tasks:
        # logger.info('KMeans Evaluation for %s tries.' % n_average)
            for _ in range(n_average): # n_average 횟수만큼 반복하여 평균을 계산
                # clustering
                pre = time()
                clf = KMeans(n_clusters=len(label_set), max_iter=500, tol=1e-5, n_init=10) # 경고알람 제거를 위해 n_init=10 파라미터 추가
                # print("=====clustering features=====")
                # print(features, len(features), features.shape)
                clf.fit(features)
                y_pred = clf.predict(features) # 클러스터링 결과
                clustering_time += (time() - pre) / n_average

                ## RI
                pre = time()
                RI += adjusted_rand_score(labels, y_pred) / n_average
                RI_time += (time() - pre) / n_average

                ## NMI
                pre = time()
                NMI += normalized_mutual_info_score(labels, y_pred) / n_average
                NMI_time += (time() - pre) / n_average

                ## acc
                pre = time()
                acc += get_accuracy(labels, y_pred) / n_average
                acc_time += (time() - pre) / n_average

                ## purity
                pre = time()
                purity += purity_score(labels, y_pred) / n_average
                purity_time += (time() - pre) / n_average
        
        # scoring
        if 'semantic_relatedness' in tasks or 'session_retrieval' in tasks:
            pre = time()
            scores_from_subject, scores_from_model = precalculate_scores_from_subject_and_model(y_true=labels, features=features) # 이 함수 확인 필요
            scores_from_subject = skip_diag_strided(scores_from_subject)
            scores_from_model = skip_diag_strided(scores_from_model)
            scoring_time += (time() - pre)

            # Semantic Relatedness
            if 'semantic_relatedness' in tasks:
                pre = time()
                SR = semantic_relatedness_precise(y_true=None,
                                                features=None,
                                                scores_from_subject=scores_from_subject,
                                                scores_from_model=scores_from_model,
                                                dtype=dtype)
                SR_time = time() - pre

            # Session Retrieval
            if 'session_retrieval' in tasks:
                pre = time()
                rankings = get_rankings(scores_from_model, dtype=dtype) # 각 대화에 대해 코사인 유사도가 높은 대화 순으로 순위를 매기는 작업
                ranking_time = time() - pre

                ## MRR
                pre = time()
                # MRR = mean_reciprocal_rank(scores_from_subject, rankings, dtype=dtype)        # Wrong
                MRR = mean_reciprocal_rank(scores_from_subject, scores_from_model, dtype=dtype)
                mrr_time = time() - pre

                ## MAP
                pre = time()
                MAP = mean_average_precision(scores_from_subject, rankings, dtype=dtype)
                map_time = time() - pre
        
        # Visualization
        if 'visualization' in tasks:
            if tsne_visualization_output is not None:
                pre = time()
                tsne_visualization(features, labels, output_filename=tsne_visualization_output)
                visualization_time = time() - pre
                logger.info('Visualization done. Time cost: %ss' % visualization_time)

        # Alignment & Uniformity
        if 'align_uniform' in tasks:
            pre = time()
            if gpu_features is not None:
                normalized_features = F.normalize(gpu_features, p=2, dim=-1)
                alignment, adjusted_alignment, uniformity = align_uniform(normalized_features=normalized_features,
                                                                        labels=gpu_labels,
                                                                        device=gpu_features.device)
            else:
                normalized_features = F.normalize(torch.tensor(features), p=2, dim=-1).cpu()
                alignment, adjusted_alignment, uniformity = align_uniform(normalized_features=normalized_features,
                                                                        labels=labels,
                                                                        device='cpu')
            align_uniform_time = time() - pre
            # logger.info("Align_Uniform Time Costs: %s " % align_uniform_time)

        if logger is not None:
            logger.info("\nclustering_task [%s]: RI: %s NMI: %s Acc: %s Purity: %s" % (note, RI, NMI, acc, purity))
            logger.info("\nSemantic Relatedness [%s]: SR: %s" % (note, SR))
            logger.info("\nSession Retrieval [%s]: MRR: %s MAP: %s" % (note, MRR, MAP))
            logger.info("\nRepresentation_Evaluation [%s]: Alignment: %.6f Alignment (adjusted): %.6f Uniformity: %.6f" % (note, alignment, adjusted_alignment, uniformity))

            tb = PrettyTable()
            tb.field_names = ['', 'RI', 'NMI', 'Acc', 'Purity', 'SR', 'MRR', 'MAP', 'Alignment', 'Adjusted Alignment', 'Uniformity']
            tb.add_row(['Metrics'] + ['%.2f' % (v * 100) for v in [RI, NMI, acc, purity, SR, MRR, MAP]] + ['%.2f' % v for v in [alignment, adjusted_alignment, uniformity]])
            tb.add_row(['Times'] + ['%.2f s' % v for v in [clustering_time/4 + RI_time,
                                                        clustering_time/4 + NMI_time,
                                                        clustering_time/4 + acc_time,
                                                        clustering_time/4 + purity_time,
                                                        scoring_time/3 + SR_time,
                                                        scoring_time/3 + ranking_time/2 + mrr_time,
                                                        scoring_time/3 + ranking_time/2 + map_time,
                                                        align_uniform_time/3,
                                                        align_uniform_time/3,
                                                        align_uniform_time/3]])
            logger.info('\n' + tb.__str__()) 
        
        return EvaluationResult(
            RI=RI,
            NMI=NMI,
            acc=acc,
            purity=purity,
            SR=SR,
            MRR=MRR,
            MAP=MAP,
            alignment=alignment,
            adjusted_alignment=adjusted_alignment,
            uniformity=uniformity
        )

    # if __name__ == '__main__':
    #     # y_true = np.array([0, 1, 1, 2, 0])
    #     # features = np.array([[1, 0, 0],
    #     #                      [0, 1, 0],
    #     #                      [0, 1, 0],
    #     #                      [0, 0, 1],
    #     #                      [1, 0, 0]])
    #     # y_true = np.random.randint(0, 5, size=(5))
    #     # # features = np.random.rand(1000, 1000)
    #     # leak_matrix = np.random.random(size=(5, 5))
    #     # features = leak_matrix[y_true]
    #     #
    #     # # SR = semantic_relatedness(y_true, features)
    #     # # print(SR)
    #     #
    #     # session_retrieval_result(y_true, features)

    #     # er1 = EvaluationResult(RI=1, acc=0.)
    #     # er2 = EvaluationResult(RI=2, acc=-1)
    #     pass
    #     n_sample = 1000
    #     device = 'cpu'
    #     # total_loss = 0.
    #     # for i in tqdm(range(n_try)):
    #     #     x = torch.rand(size=(3000, 768)).cuda()
    #     #     y = torch.rand(size=(3000, 768)).cuda()
    #     #     label = torch.randint(0, 2, size=(3000, 1)).cuda()
    #     #
    #     #     aloss, _ = align_loss(x.cpu(), y.cpu(), label.cpu())
    #     #     # aloss, _ = align_loss(x, y, label)
    #     #     total_loss += aloss
    #     # print(total_loss)
    #     pre = time()
    #     features = torch.rand(size=(n_sample, 768))
    #     normalized_features = F.normalize(features, p=2, dim=-1).to(device)

    #     alignment, adjusted_alignment, uniformity = align_uniform(normalized_features,
    #                                                             labels=torch.randint(0, 5, size=(n_sample, 1)),
    #                                                             device=device)
    #     print('Cost time: %s' % (time() - pre))
        