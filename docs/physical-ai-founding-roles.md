# Physical AI Founding Roles — the JD-grounded org OpenOPC staffs

## What this document is

A **robot that learns to do physical work** is not built by one genius — it's built by a small
team wired into a loop: people teach the robot (data), the robot learns a brain (a model), the
brain is tested against reality (sim + eval), the robot goes to a real job site (deployment), and
what happens there becomes the next batch of teaching data. That loop is the **robot-learning
flywheel**, and every serious Physical AI company is organized around it.

This doc pins that idea to real job descriptions so OpenOPC can *staff the loop* — spin up an AI
company whose roles mirror how a top Physical AI startup actually hires. It is the research behind
two shipped artifacts:

- the **`physical-ai-robotics-company`** architecture preset (`opc/market/builtin_presets/physical_ai_robotics_company.yaml`) — the industry-generalized org,
- the **RoboForce Titan** saved org (`.opc/config/company_orgs/org_roboforce-titan_config.yaml`) — one concrete instance of that preset, used as the prototype.

The anchor role is the **Founding AI Native Lead** — a real, currently-open RoboForce position
(their "Strategy & AI-Native" department). This repo is built to make *that* role runnable as an
AI-native company: one person plus a staffed loop of role-agents.

## The prototype: RoboForce (verified)

- **What they build.** RoboForce (founded 2023, Milpitas CA; founder/CEO Leo Ma, ex-CMU) builds
  **Titan**, an AI robot for "dull, dirty, dangerous" industrial work — solar, mining,
  manufacturing, space. Titan does contact-rich manipulation (Pick, Place, Press, Twist, Connect)
  at ~1mm precision with an 8-hour runtime, on modular wheeled/tracked bases. Their thesis is
  **"Domain Intelligence" via tight AI–hardware co-optimization**. Raised ~$67M total (YZi Labs
  led the $52M round; Jerry Yang, Myron Scholes among investors).
- **How they hire** (their live careers board, verified — the role taxonomy that seeds our org):

  | RoboForce department | Open roles | Maps to our archetype |
  |---|---|---|
  | Strategy & AI-Native | **Founding AI Native Lead** | `founding_ai_native_lead` (final decider) |
  | AI / Foundation Models | AI Research Scientist, Foundation Models · AI Research Engineer, Embodied Systems Lead · AI Resident | `robot_learning_lead`, `foundation_model_scientist` |
  | AI / Data | AI Research Engineer, Data Infrastructure · Data Collection Operator · Model Evaluation Operator | `data_engine_engineer` |
  | Robotics Software | Robotics Engineer, Manipulation · Robotics Engineer, Motion Planning · Robotics Software Engineer · Technical Lead, Simulation · Test Infrastructure | `manipulation_engineer`, `motion_planning_engineer`, `sim2real_engineer`, `ai_infra_reliability_engineer` |
  | Robotics Hardware | Embedded (Devices/Platform) · Mechanical · Manufacturing DFM | co-design context (informs the loop; not a separate agent role) |

  *Salary signal for the anchor role: Founding AI Native Lead, $120K–$180K, Milpitas CA (verified from the posting metadata).* The **full prose JD text lives behind Greenhouse and was not machine-readable**; responsibilities below are reconstructed from the title, department, and the surrounding role set — marked *inferred*.

## The industry pattern (5–8 companies, verified thesis lines)

The same loop, different robot bodies. This is why the preset generalizes beyond RoboForce:

| Company | Thesis (verified) | What it proves about the loop |
|---|---|---|
| **Physical Intelligence (π)** | One learning model to control *any* robot on *any* task; ~$400M raised; π0 uses **flow-matching** action generation. | Foundation-model-first robot learning is a fundable category. |
| **Figure AI** | Humanoid running **Helix** (a VLA), in paid BMW Spartanburg pilots. | The loop ends in real industrial deployment, not demos. |
| **Skild AI** | A robot "foundation model" / general robot brain. | Same brain-across-bodies bet as π. |
| **1X Technologies** | Neo humanoid; teleoperation-heavy data collection into consumer pilots. | Teleop data ops is a first-class function. |
| **Tesla Optimus** | Vertically integrated humanoid; imitation learning from human video. | Data engine + hardware co-design under one roof. |
| **NVIDIA (Isaac / GR00T)** | Robot foundation models + **sim** (Isaac Sim) as the training substrate. | Sim2real is core infrastructure, not a nice-to-have. |
| **Agility (Digit) / Apptronik (Apollo)** | Fleets in GXO/Amazon/Mercedes; **Robot-as-a-Service** + fleet cloud (Agility Arc). | Deployment + fleet telemetry closes the loop back to data. |

Cross-company skill clusters that recur in nearly every JD: **VLA / robot foundation models**,
**imitation learning + RL**, **teleoperation data pipelines** (RLDS-style (image, instruction,
action) tuples), **sim2real** (Isaac Sim / MuJoCo, domain randomization), **3D perception / SLAM**,
**manipulation & motion planning**, **real-time control**, **C++/Python/ROS2**, and
**fleet deployment / MLOps**.

## The role archetypes (what each agent owns)

Each maps 1-to-1 to a role in the preset. *Verified* = drawn from a real open title; *inferred* =
reconstructed responsibilities.

1. **Founding AI Native Lead** *(verified title — RoboForce)* — final decider. Owns the capability
   thesis: which physical task to crack first, what "done" means (a success-rate bar on real
   hardware), and the go/no-go on deployment. Runs the company as an AI-native loop. *This is the
   role this repo is built around.*
2. **Robot Learning Lead** *(verified — "Embodied Systems Lead")* — owns the learning strategy:
   imitation-vs-RL, data budget, the training curriculum, and the eval bar the policy must clear.
3. **Foundation Model Scientist** *(verified — "Foundation Models")* — trains the VLA / policy
   (behavior cloning, flow-matching/diffusion policies, RL fine-tune); owns model architecture and
   the (obs → action) mapping.
4. **Data Engine Engineer** *(verified — "Data Infrastructure" + "Data Collection Operator")* —
   owns the teleoperation → curated-dataset pipeline: collection protocol, RLDS formatting,
   diversity/coverage, dedup, and the data quality gate.
5. **Sim2Real Engineer** *(verified — "Technical Lead, Simulation")* — owns the simulator of the
   *specific* robot + task, domain randomization, and the sim→real transfer eval.
6. **Perception Engineer** *(inferred — implied by manipulation/motion stack)* — 3D vision, state
   estimation, SLAM; turns raw sensors into the world state the policy consumes.
7. **Manipulation Engineer** *(verified — "Manipulation")* — grasping and contact-rich skills
   (Titan's Press/Twist/Connect class of tasks).
8. **Motion Planning Engineer** *(verified — "Motion Planning")* — trajectories, collision-free
   paths, real-time control of the arm/base.
9. **Deployment & Field Engineer** *(inferred from industry RaaS pattern — Agility/Apptronik)* —
   takes the robot to the real site, runs the pilot, owns on-site reliability and the RaaS surface.
10. **AI Infra & Reliability Engineer** *(verified — "Test Infrastructure")* — eval harness,
    fleet telemetry, safety gates, uptime; the reviewer on the sim2real and deployment gates.

## What "Founding AI Engineer" means here — the common denominator

Across all these companies the founding/staff AI role is defined by **end-to-end ownership of the
loop, not a slice of it**: 0→1 scope, comfort moving from a research policy to a robot doing real
work on a real site, and willingness to own the data engine and the deployment, not just the model.
OpenOPC encodes exactly that: the `founding_ai_native_lead` is the final decider over a DAG that
runs data → model → sim/eval → integration → deployment → telemetry → back to data.

## Sources

- RoboForce careers & Greenhouse board: https://www.roboforce.ai/careers · https://job-boards.greenhouse.io/roboforce
- RoboForce funding/product (Titan, $52M): https://techfundingnews.com/as-industrial-labour-shortages-grow-roboforce-grabs-52m-for-ai-robots/ · https://www.therobotreport.com/roboforce-introduces-titan-mobile-manipulator-raises-5m-more-funding/ · https://www.prnewswire.com/news-releases/roboforce-introduces-ai-robot-titan-for-real-world-industrial-deployment-and-announces-15m-in-total-funding-302458546.html
- Physical Intelligence (π0, flow matching): https://www.physicalintelligence.company/blog/pi05 · https://www.pi.website/
- Figure (Helix, BMW), 1X, Apptronik, Agility, industry stack: https://www.figure.ai/careers · https://sacra.com/research/figure-vs-apptronik-vs-agility-robotics/ · https://www.agilityrobotics.com/careers · https://www.kore1.com/hire-robotics-engineers-2026/
- VLA / robot foundation model background: https://rohitbandaru.github.io/blog/Foundation-Models-for-Robotics-VLA/ · https://www.roboticscenter.ai/blog/physical-ai-2026-guide

*Verified claims are cited above; role responsibilities marked "inferred" are reconstructions from
public titles/departments where the full prose JD was behind an application wall.*
