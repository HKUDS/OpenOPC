---
name: physical_ai
description: "Domain knowledge for Physical AI / robotics roles — the robot-learning flywheel, VLA foundation models, teleop data engines, sim2real, and field deployment"
domain:
  - physical-ai
  - robotics
  - embodied-ai
  - robot-learning
  - vla
  - sim2real
  - manipulation
  - deployment
trigger: "When a role works on a Physical AI / robotics capability — training a robot policy, building a data engine, running sim2real eval, integrating perception/manipulation/motion, or deploying to a real site"
always_on: false
---

# Physical AI Skill

## The mental model: the robot-learning flywheel

A robot that learns to do physical work is built by turning one loop as fast as possible:

**teach → learn → test → deploy → observe → teach again.**

1. **Teach (data engine).** Humans demonstrate the task by teleoperation. Each demo becomes a
   `(image, instruction, action)` tuple (RLDS-style). Coverage and diversity beat raw count.
2. **Learn (foundation model / policy).** Train a Vision-Language-Action (VLA) policy that maps
   observations to actions — behavior cloning first, then flow-matching / diffusion policies for
   smooth contact-rich motion, optionally RL fine-tuning.
3. **Test (sim2real + eval).** Evaluate in a simulator of *this specific robot and task* (Isaac Sim
   / MuJoCo) with domain randomization, then on held-out real trials. The number that matters is
   **task success rate on real hardware**, not loss curves.
4. **Deploy (field).** Run a pilot at the real site. Own reliability, safety envelopes, and the
   operator handoff (Robot-as-a-Service).
5. **Observe (fleet telemetry) → teach again.** Field telemetry surfaces the failure modes that
   become the next batch of teaching data. The loop closes.

The whole point of a Physical AI company is to make this loop turn quickly and safely. Every role
below owns one arc of it.

## Ground truth rules (do not skip)

- **Capability is measured on real hardware.** State the target task, the success-rate bar, and the
  cycle time / precision target up front. A model metric in isolation is not evidence.
- **No green metric, no ship.** A policy advances past a gate only with held-out success rates. A
  deployment advances only within a verified safety envelope. Reject anything that lacks this.
- **Data quality gates training.** Deduplicate, check coverage across objects/poses/lighting, and
  reject low-quality teleop before it pollutes the dataset.
- **Sim is for failing cheaply.** Invest in a high-fidelity model of the specific robot+task; a
  generic sim transfers poorly. Predict real-world success in sim before spending hardware time.
- **Safety is a hard constraint, not a metric to trade off.** Collision-free planning and force
  limits are envelopes; a run that violates them fails regardless of task success.

## Role-to-arc map

| Role | Owns which arc | Key methods / tools |
|---|---|---|
| Founding AI Native Lead | the whole loop + go/no-go | capability thesis, deployment decision |
| Robot Learning Lead | learn strategy | imitation vs RL, data budget, eval bar |
| Foundation Model Scientist | learn (train) | VLA, behavior cloning, flow-matching/diffusion, RL fine-tune |
| Data Engine Engineer | teach (data) | teleop protocol, RLDS formatting, dedup, coverage |
| Sim2Real Engineer | test (sim) | Isaac Sim / MuJoCo, domain randomization, transfer eval |
| Perception Engineer | test/deploy (see) | 3D vision, SLAM, state estimation, calibration |
| Manipulation Engineer | deploy (act) | grasping, contact-rich skills, force control |
| Motion Planning Engineer | deploy (move) | trajectory optimization, collision-free planning, real-time control |
| Deployment & Field Engineer | deploy (site) | field pilot, RaaS, on-site reliability |
| AI Infra & Reliability Engineer | observe (gate) | eval harness, fleet telemetry, safety gates, uptime |

## Vocabulary anchor (so a newcomer can follow the team)

- **VLA** = Vision-Language-Action model — one model that sees, reads an instruction, and outputs
  robot actions.
- **Imitation learning / behavior cloning** = learn the task by copying human demonstrations.
- **Teleoperation** = a human drives the robot (VR controllers / kinesthetic) to generate demos.
- **RLDS** = the standard `(observation, instruction, action)` dataset format for robot learning.
- **Sim2real** = making a policy trained/tested in simulation work on the physical robot.
- **Domain randomization** = varying sim conditions (lighting, physics, textures) so the policy
  survives the messy real world.
- **RaaS** = Robot-as-a-Service — the robot is delivered and operated as an ongoing service, with a
  fleet-management cloud behind it.
