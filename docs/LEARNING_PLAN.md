# Learning plan: deep learning to AI agents (free, 8 weeks)

A self-paced plan that covers the same ground as paid "deep learning to AI agents" bootcamps, using free material, and ties each topic to a JobPilot step so the learning ends up in the portfolio.

**Rhythm:** about 5–6 hours a week, next to job applications (applications come first).
One fixed slot a week, for example Saturday morning. At the end of each week, explain what I learned in my own words and answer a few quiz questions (with Claude as a study partner). Tick the boxes as I go.

| Week | Topic | Free resource | Apply it in JobPilot |
|---|---|---|---|
| 1 | How a neural network learns: neurons, loss, gradient descent, backpropagation | [Andrej Karpathy, Neural Networks: Zero to Hero](https://karpathy.ai/zero-to-hero.html), lecture 1 (micrograd) | Explain backpropagation in the learning log |
| 2 | Training in practice: layers, activations, overfitting, train/validation split | Zero to Hero, lecture 2 (makemore part 1), or [Kaggle Learn: Intro to Deep Learning](https://www.kaggle.com/learn/intro-to-deep-learning) | — |
| 3 | Practical deep learning and transfer learning | [fast.ai Practical Deep Learning for Coders](https://course.fast.ai/), lessons 1–2 | — |
| 4 | NLP models and fine-tuning a pretrained model | fast.ai lesson 4 (NLP), [Hugging Face LLM Course](https://huggingface.co/learn/llm-course) chapters 1–2 | Plan the relevance model on my 👍/👎 labels |
| 5 | Transformers, tokenizers, embeddings | Hugging Face LLM Course chapter 3 | Roadmap step 7: embeddings with pgvector |
| 6 | Retrieval and RAG | [DeepLearning.AI short courses](https://www.deeplearning.ai/short-courses/) on embeddings, vector search and RAG | Step 7: semantic job search |
| 7 | AI agents and tool use | [Hugging Face AI Agents Course](https://huggingface.co/learn/agents-course), DeepLearning.AI agent courses | Step 8: Claude agent + MCP server over the API |
| 8 | Evaluating LLM apps: test sets, metrics, hallucinations, cost | DeepLearning.AI evaluation courses | Step 9: evals, precision@10 of keyword vs embeddings vs fine-tuned model |

## Checklist

- [ ] Week 1: neural network basics, backpropagation
- [ ] Week 2: training, overfitting, validation
- [ ] Week 3: practical deep learning, transfer learning
- [ ] Week 4: NLP and fine-tuning
- [ ] Week 5: transformers and embeddings
- [ ] Week 6: RAG and vector search
- [ ] Week 7: AI agents and tool use
- [ ] Week 8: evaluating LLM applications

## The portfolio piece this builds towards

Once the Telegram 👍/👎 buttons have collected about 150–200 labels, train and compare three ways to rank jobs for me, measured on the same labelled set (`labelled_jobs`):

1. the current keyword score
2. embeddings plus a small classifier
3. a fine-tuned small transformer

Report precision@10 for each, honestly, including the labelling bias (only alerted jobs get labels).

## Optional certificate

[Microsoft Azure AI Fundamentals (AI-900)](https://learn.microsoft.com/en-us/credentials/certifications/azure-ai-fundamentals/): the learning path is free on Microsoft Learn; the exam is paid (look out for free voucher events).
