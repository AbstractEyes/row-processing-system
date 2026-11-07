# row-processing-system

A controller that writes dataset rows through model APIs, checks them, hands them to review
agents, collects their verdicts and ships accepted rows to a private Hugging Face dataset
repository. It runs on a Colab CPU runtime (and locally for tests) and can stop at any moment
and resume where it left off.

Status: skeleton (version 0.1.0). The provider adapters are stubs.

The full documentation is added as the modules land.
