import os, glob, json, argparse
from ast import literal_eval
from functools import partial
from tqdm.auto import tqdm
from pathlib import Path

import wandb
import pandas as pd

from datasets import load_from_disk

import evaluate
import torch
from transformers import GenerationConfig, AutoTokenizer, AutoModelForCausalLM
from transformers.integrations import WandbCallback

def str2bool(v):
    "Fix Argparse to process bools"
    if isinstance(v, bool):
        return v
    if v.lower() == 'true':
        return True
    elif v.lower() == 'false':
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected.')

def parse_args(config):
    print("Running with the following config")
    parser = argparse.ArgumentParser(description='Run training baseline')
    for k,v in config.__dict__.items():
        parser.add_argument('--'+k, type=type(v) if type(v) is not bool else str2bool, 
                            default=v, 
                            help=f"Default: {v}")
    args = vars(parser.parse_args())
    
    # update config with parsed args
    for k, v in args.items():
        try:
            # attempt to eval it it (e.g. if bool, number, or etc)
            attempt = literal_eval(v)
        except (SyntaxError, ValueError):
            # if that goes wrong, just use the string
            attempt = v
        setattr(config, k, attempt)
        print(f"--{k}:{v}")

def load_jsonl(file_path):
    data = []
    with open(file_path, 'r') as file:
        for line in file:
            data.append(json.loads(line))
    return data

def load_ds_from_artifact(at_address, at_type="dataset"):
    "Load a HF dataset from a W&B artifact"
    artifact = wandb.use_artifact(at_address, type=at_type)
    artifact_dir = artifact.download()
    return load_from_disk(artifact_dir)


def model_class(model_path):
    if list(model_path.glob("*adapter*")):
        return AutoPeftModelForCausalLM
    return AutoModelForCausalLM

def load_model_from_artifact(model_at, **model_kwargs):
    """Load model and tokenizer from W&B
    If the tokenizer is not present, we load a pretrained from the hub"""
    if not wandb.run:
        from wandb import Api
        api = Api()
        artifact = api.artifact(model_at, type="model")
    else:
        artifact = wandb.use_artifact(model_at, type="model")
    artifact_dir = Path(artifact.download())
    
    model = model_class(artifact_dir).from_pretrained(
        artifact_dir,
        **model_kwargs)
    try:
        tokenizer = AutoTokenizer.from_pretrained(artifact_dir)
        tokenizer.pad_token = tokenizer.eos_token
    except:
        model_id = artifact.metadata.get("model_id")
        if model_id is None:
            producer_run = artifact.logged_by()
            config = producer_run.config
            model_id = config.get("model_id")
        print(f"Tokenizer not found.\nLoading a pretrained tokenizer from the HF-hub: {model_id}")
        tokenizer = AutoTokenizer.from_pretrained(model_id)
        tokenizer.pad_token = tokenizer.eos_token
    return model, tokenizer, artifact_dir



def get_latest_file(path):
    files = glob.glob(path + "/*")
    latest_file = max(files, key=os.path.getctime)
    print("Latest created file is: ", latest_file)
    return latest_file

def save_model(model, model_name, models_folder="models", log=False, **artifact_kwargs):
    """Save the model to wandb as an artifact
    Args:
        model (nn.Module): Model to save.
        model_name (str): Name of the model.
        models_folder (str, optional): Folder to save the model. Defaults to "models".
    """
    model_name = f"{wandb.run.id}_{model_name}"
    file_name = Path(f"{models_folder}/{model_name}")
    file_name.parent.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(file_name, safe_serialization=True)
    # save tokenizer for easy inference
    tokenizer = AutoTokenizer.from_pretrained(model.name_or_path)
    tokenizer.save_pretrained(file_name)
    if log:
        # if wandb.__version__ > "0.15.2":
        #     wandb.log_model(file_name, model_name)
        # else:
        at = wandb.Artifact(model_name, type="model", **artifact_kwargs)
        at.add_dir(file_name)
        wandb.log_artifact(at)

        
def to_gpu(tensor_dict):
    return {k: v.to('cuda') for k, v in tensor_dict.items()}

class Accuracy:
    "A simple Accuracy function compatible with HF models"
    def __init__(self):
        self.count = 0
        self.tp = 0.
    def update(self, logits, labels):
        logits, labels = logits.argmax(dim=-1).view(-1).cpu(), labels.view(-1).cpu()
        tp = (logits == labels).sum()
        self.count += len(logits)
        self.tp += tp
        return tp / len(logits)
    def compute(self):
        return self.tp / self.count

def flat_cross_entropy(x, y):
    "A Flat CrossEntropy" 
    return torch.nn.functional.cross_entropy(x.view(-1, x.shape[-1]), y.view(-1))

def token_accuracy(eval_preds):
    token_accuracy = evaluate.load("accuracy")
    logits, labels = eval_preds
    predictions = np.argmax(logits, axis=-1)
    return token_accuracy.compute(predictions=predictions, references=labels)


def _generate(prompt, model, tokenizer, gen_config):
    tokenized_prompt = tokenizer(prompt, return_tensors='pt')['input_ids'].cuda()
    with torch.inference_mode():
        output = model.generate(tokenized_prompt, 
                                generation_config=gen_config)
    return tokenizer.decode(output[0][len(tokenized_prompt[0]):], skip_special_tokens=True)


class LLMSampleCB(WandbCallback):
    def __init__(self, trainer, test_dataset, num_samples=10, max_new_tokens=256, log_model="checkpoint"):
        super().__init__()
        self._log_model = log_model
        self.sample_dataset = test_dataset.select(range(num_samples))
        self.model, self.tokenizer = trainer.model, trainer.tokenizer
        self.gen_config = GenerationConfig.from_pretrained(trainer.model.name_or_path,
                                                           max_new_tokens=max_new_tokens)
    def generate(self, prompt):
        tokenized_prompt = self.tokenizer(prompt, return_tensors='pt')['input_ids'].cuda()
        with torch.inference_mode():
            output = self.model.generate(input_ids=tokenized_prompt, generation_config=self.gen_config)
        return self.tokenizer.decode(output[0][len(tokenized_prompt[0]):], skip_special_tokens=True)
    
    def samples_table(self, examples):
        records_table = wandb.Table(columns=["prompt", "generation"] + list(self.gen_config.to_dict().keys()))
        for example in tqdm(examples, leave=False):
            prompt = example["text"]
            generation = self.generate(prompt=prompt)
            records_table.add_data(prompt, generation, *list(self.gen_config.to_dict().values()))
        return records_table
        
    def on_evaluate(self, args, state, control,  **kwargs):
        super().on_evaluate(args, state, control, **kwargs)
        records_table = self.samples_table(self.sample_dataset)
        self._wandb.log({"sample_predictions":records_table})

def param_count(model):
    params = sum([p.numel() for p in model.parameters()])/1_000_000
    trainable_params = sum([p.numel() for p in model.parameters() if p.requires_grad])/1_000_000
    print(f"Total params: {params:.2f}M, Trainable: {trainable_params:.2f}M")
    return params, trainable_params
    
def freeze(model, n_freeze, freeze_embed):
    if n_freeze == -1:
        return model

    def _find_mod(model, module_name="layers"):
        for name, mod in model.named_modules():
            if name.endswith(module_name):
                return mod
    # freeze layers (disable gradients)
    for param in model.parameters(): param.requires_grad = False
    for param in model.lm_head.parameters(): param.requires_grad = True

    layers = _find_mod(model, "layers")
    for param in layers[n_freeze:].parameters(): param.requires_grad = True

    # Just freeze embeddings for small memory decrease
    if freeze_embed:
        embed_tokens = _find_mod(model, "embed_tokens")
        embed_tokens.weight.requires_grad_(False);