# Two-paper development evidence pilot

Manual selection by a developer exposed to prior reviews. Not blind, held-out, or a validation of automated passage selection. No model execution has occurred.

Both packets preserve claims and caveats. Figures and unselected content are omitted; table 2 in TELLER is retained as full-width text. Exact citation validation establishes location, not semantic entailment.


## crystallization: 6042 characters


### p1: page/region 2-right, offsets 505:2082

Stage 2: capture. On verified successful resolution, the
successful path is extracted into a reusable Type 3 template.
The extraction algorithm parses the trace into an ordered list
of tool calls, detects the branch conditions the agent acted on,
infers input and output schemas per step, builds a directed
acyclic graph of tool dependencies, parameterizes instance-
specific values such as device identifiers and timestamps, and
marks human-approval points as explicit gates.
Stage 3: promotion to hybrid. After repeated successful
runs of the template, trace analysis identifies steps where the
LLM consistently produces the same classification (replaced
by a deterministic rule) and steps where reasoning varies but
the outcome is stable (replaced by a scoped, single-purpose
prompt). Acceptance tests are generated automatically from
the successful traces, and the candidate Type 2 playbook must
pass them.
Stage 4: promotion to deterministic. After further suc-
cessful hybrid runs without LLM disagreement, remaining
LLM steps whose output is drawn from a finite set, or whose
decision boundary can be expressed as a rule, are replaced with
deterministic equivalents. The final Type 1 playbook needs no
runtime tokens and continues to be validated by the Stage 3
acceptance tests.
Promotion is gated by the criteria in Table II. Crucially,
autonomy is attached to the specific playbook class and action
type, based on its evidence, rather than to the capability
of the underlying model. A more capable model does not
automatically earn more autonomy; a track record does.


### p2: page/region 2-right, offsets 0:504

TABLE II
EVIDENCE-BASED PROMOTION CRITERIA (DEFAULTS; CONFIGURABLE).
Transition Requirements
Type 3 → 2 ≥10 successful runs; zero safety violations;
≥90% of runs produce the same action se-
quence; all auto-generated acceptance tests
pass; no human override in the recent window
Type 2 → 1 ≥50 successful hybrid runs; LLM classifica-
tion consistency ≥99%; the deterministic rule
covers all observed input variation; full re-
gression suite passes without the LLM; human
review of the deterministic logic


### p3: page/region 3-right, offsets 369:1153

VII. DEMOTION AND CONTINUOUS DISCOVERY
Crystallization is not one-way. Each promoted playbook is
monitored, and a circuit breaker demotes it to a higher execu-
tion type on execution failure, safety violation, or acceptance-
test regression. In production, a deterministic playbook once
broke after a firmware update changed a command’s output
format; the deterministic parser failed, the system demoted the
playbook to hybrid so the LLM could handle the new format,
and after a run of clean executions it was re-promoted. This
gives the platform the reliability of deterministic automation
for stable patterns and the adaptability of agents for change,
without a human deciding when to switch. Genuinely novel
incidents always enter as Type 3, so the discovery pipeline
never closes.


### p4: page/region 3-right, offsets 1154:1762

VIII. PRODUCTION EVALUATION
We deployed progressive crystallization in a production
agentic platform for cloud network operations that resolves
incidents across a large managed network and handles tens
of thousands of incidents per month [2]. We report three
observations.
The mix shifts toward determinism. At launch, essentially
all executions were Type 3. Over eight months the share of
deterministic (Type 1) executions rose from zero to about 45
percent, with roughly 30 percent hybrid and 25 percent agent-
orchestrated (Fig. 3). The ratio of Type 1:2:3 executions is a
useful platform-maturity metric.


### p5: page/region 4-left, offsets 214:1922

Cost falls as volume rises. Over the same period, per-
incident agent cost fell by more than 70 percent while incident
volume roughly doubled. This is the central economic claim of
the paper realized in production: the platform got cheaper as
it did more, because it stopped paying for inference on work
it had already learned.
Autonomy and quality held. The platform resolves over
90 percent of common incident categories autonomously, mean
time to resolution fell from hours to minutes, and the false-
positive remediation rate stayed under 5 percent with no
customer-visible impact. Crystallization did not trade quality
for cost; the deterministic paths are the ones that had already
proven reliable as agent runs.
IX. DISCUSSION, LIMITATIONS, AND THREATS TO
VALIDITY
Results come from a single organization and operational
domain, so specific thresholds and ratios should be re-derived
elsewhere; the lifecycle itself is domain-agnostic. Crystalliza-
tion assumes recurring patterns, so its benefit is smaller in
environments dominated by genuinely novel, one-off incidents,
where most executions will remain Type 3. The economic
figures are platform-level observations rather than a controlled
comparison, and the eight-month window reflects one maturity
trajectory. Automatic promotion depends on the quality of
the acceptance tests generated from traces; a pattern that is
under-observed can be promoted prematurely, which is why
demotion and human review of the final deterministic logic
are part of the design. Finally, extraction quality depends on
the richness of execution traces; sparse or poorly structured
logging limits what can be crystallized, echoing a known
dependency in process mining.


### p6: page/region 1-right, offsets 1467:2328

II. RELATED WORK
LLM agents that reason and act through tools were popu-
larized by ReAct [3] and Toolformer [4], and the design space
is surveyed in [5], [6]. Applying these agents to operations is
the focus of AIOps [7] and its LLM-era successors [1], [2].
Prior work largely treats the agent as the permanent execution
engine. Cost-reduction efforts such as FrugalGPT [8] lower
per-call cost through model cascades and routing, but the
system remains probabilistic and never eliminates inference
for solved problems. Our contribution is orthogonal and com-
plementary: rather than making each inference cheaper, we
remove inference entirely for work that has been proven, by
extracting deterministic workflows from execution traces. The
extraction step draws on process mining [9], which recovers
process models from event logs; here the event logs are agent


## teller: 6848 characters


### p1: page/region 7-left, offsets 1559:1853

The dataset contains 300 request traces from five inference
settings: SGLang, Torch FSDP, vLLM v0, vLLM v1 offline, and
vLLM v1 online. These settings cover both framework diversity
and deployment-mode diversity. In total, the dataset contains 2,482
execution steps and 5.63M cross-layer nodes.


### p2: page/region 7-right, offsets 1191:1712

We randomly shuffle request traces with a fixed seed and allocate
80%, 10%, and 10% to training, validation, and test sets. All steps
from one request remain in the same split, so no step from a test
request appears during training. The splits are request-disjoint.
However, correlated traces from the same workload, run, or fault-
injection campaign can still cross splits, so the reported numbers
should be interpreted as in-distribution estimates. Workload-, run-,
or injection-disjoint evaluation remains future work.


### p3: page/region 7-right, offsets 1713:2768

4.3 Baselines
We compare TELLER against a diverse set of existing methods
widely used in anomaly detection and log-based diagnosis.
Classical unsupervised methods. We include KMeans, DBSCAN,
Isolation Forest, and Gaussian Mixture Models (GMM), which rep-
resent common unsupervised baselines for clustering- and density-
based anomaly detection [10, 12, 20, 29].
Classical supervised methods. We include SVM, Random Forest,
and XGBoost as representative supervised learners for structured
anomaly classification [4, 7, 33]. Here the comparison uses their
standard statistical feature setting. To ensure a fairer comparison,
we also feed flattened operator/parent-name trace features and
operator-count features to these classical baselines, and the results
are reported in Table 4.
Log-based and sequence/deep baselines. We compare against Ro-
bustlog, LAnoBERT, Logs2Graphs, and MAD-GAN, which cover
robust log parsing, pretrained language-model-based log diagno-
sis, graph-based log reasoning, and GAN-style anomaly detection,
respectively [17–19, 39].


### p4: page/region 11-left, offsets 109:954

Injected versus real faults. Our dataset relies on controlled fault
injection derived from recurring patterns observed in large-scale
production clusters, providing coverage, repeatability, and precise
trace–log alignment. The released archive retains the resulting
trace timestamps, fault categories, and annotations; the mechanism-
specific injection settings are summarized in Section 4. Although
real incidents may be longer, noisier, and more entangled, these
scenarios cover representative software, CUDA/runtime, resource,
and communication failures relevant to RCA evaluation. The bal-
anced step-level dataset is therefore a controlled diagnostic bench-
mark rather than a production incident prior. Low-prior resampling
shows the expected F1/AUPRC sensitivity; threshold and alert vali-
dation at operational scale remains future work.


### p5: page/region 11-left, offsets 1237:2098

Generalization scope. End-to-end evaluation centers on a dual-
node A40 cluster with a Qwen-based serving stack; additional tests
cover SGLang, Torch FSDP-based serving, and different vLLM ver-
sions. They support engine-agnostic behavior within the tested
settings, but not behavior under larger clusters, newer GPUs, or
long-running multi-tenant workloads; those remain future work.
Split and explanation limitations. The example-level split is index-
disjoint, but samples from the same workload, run, or injection can
cross splits and share context, making in-distribution estimates
optimistic. The explanation-quality audit checks reference expla-
nations against labels and trace evidence; it is not independent,
blinded expert validation of all TELLER-generated explanations.
Run- or workload-disjoint evaluation and expert validation remain
future work.


### p6: page/region 10-right, offsets 264:1093

Tracing overhead. We use the term non-intrusive to mean that
TELLER does not modify model binaries, serving-engine source
code, CUDA libraries, or the application logic that handles requests.
Instead, it collects observability signals through NVTX ranges,
CUPTI activity records, and external trace–log alignment. This de-
sign reduces deployment friction and keeps the serving stack intact,
but it does not imply zero overhead. Enabling tracing still introduces
additional event collection, buffering, and post-processing work.
Table 10 reports this runtime cost. The measurements indicate that
the overhead is measurable but bounded in our setting.
Table 10: Tracing overhead.
Measure Value
Wall time 64.77s to 71.15s (+9.8%)
CPU utilization 217% to 236% (+8.8%)
Max resident memory +3.25%
Minor page faults +3.55%
Swap count 0


### p7: page/region 11-right, offsets 661:1318

Trace representation learning. TPE and the Trace Encoder relate
to subword compression and graph representation learning [11,
14, 15, 31]. Subword methods reduce sequence length by merging
frequent token patterns, while graph neural networks encode re-
lational structure over nodes and edges. Inference traces combine
both properties: they are long symbolic sequences, but they are also
hierarchical execution structures with timing, operator semantics,
and parent–child dependencies. TELLER therefore uses structure-
preserving merges and graph-aware encoding instead of flattening
traces into ordinary natural language or treating them as untyped
graphs.


### p8: page/region 8-full, offsets 80:1866

Table 2: Comparison with existing baselines on horizontal and vertical views. Best values in each block are boldfaced.
Step Operator set-Macro Operator set-Macro+
Method Accuracy Precision F1 Precision F1 Jaccard Precision+ F1+ Jaccard+
Horizontal view
TELLER (ours) 0.930 0.945 0.916 0.821 0.806 0.783 0.915 0.898 0.873
Robustlog 0.874 0.735 0.771 0.267 0.384 0.238 0.566 0.597 0.425
LAnoBERT 0.882 0.822 0.783 0.259 0.381 0.236 0.649 0.672 0.507
Logs2Graphs 0.825 0.603 0.684 0.254 0.383 0.237 0.514 0.596 0.424
MAD-GAN 0.819 0.636 0.706 0.246 0.382 0.236 0.460 0.579 0.407
KMeans 0.457 0.455 0.611 0.176 0.244 0.176 0.386 0.533 0.386
DBSCAN 0.457 0.457 0.628 0.187 0.259 0.187 0.408 0.567 0.408
IsolationForest 0.433 0.444 0.604 0.175 0.244 0.175 0.383 0.533 0.383
GMM 0.494 0.470 0.603 0.163 0.223 0.163 0.355 0.488 0.355
SVM 0.463 0.373 0.302 0.037 0.056 0.037 0.082 0.123 0.082
RandomForest 0.805 0.713 0.818 0.178 0.248 0.178 0.388 0.543 0.388
XGBoost 0.817 0.817 0.795 0.150 0.207 0.150 0.328 0.452 0.328
Vertical view
TELLER (ours) 0.911 0.878 0.900 0.809 0.792 0.768 0.897 0.878 0.852
Robustlog 0.873 0.725 0.703 0.310 0.447 0.288 0.454 0.559 0.388
LAnoBERT 0.875 0.675 0.726 0.214 0.340 0.205 0.442 0.572 0.401
Logs2Graphs 0.922 0.852 0.791 0.233 0.356 0.216 0.434 0.560 0.389
MAD-GAN 0.837 0.686 0.741 0.262 0.392 0.244 0.425 0.558 0.387
KMeans 0.518 0.518 0.683 0.191 0.278 0.191 0.369 0.537 0.369
DBSCAN 0.518 0.518 0.683 0.191 0.278 0.191 0.369 0.537 0.369
IsolationForest 0.537 0.528 0.691 0.191 0.278 0.191 0.369 0.537 0.369
GMM 0.549 0.545 0.644 0.149 0.219 0.149 0.288 0.422 0.288
SVM 0.701 0.634 0.776 0.191 0.278 0.191 0.369 0.537 0.369
RandomForest 0.854 0.942 0.844 0.145 0.212 0.145 0.280 0.409 0.280
XGBoost 0.780 0.930 0.746 0.119 0.174 0.119 0.230 0.336 0.230
