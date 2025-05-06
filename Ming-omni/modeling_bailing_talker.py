from dataclasses import dataclass
from typing import Optional, Tuple, List
import torch
import torch.nn as nn
import torchaudio
from hyperpyyaml import load_hyperpyyaml

from transformers import Qwen2Config, PreTrainedModel
from transformers import Qwen2ForCausalLM, AutoTokenizer
from audio_detokenizer.cli.model import AudioDetokenizerModel
from s3bpe_tokenizer import S3BpeTokenizer
from configuration_bailing_talker import BailingTalkerConfig
from transformers.utils import ModelOutput


@dataclass
class BailingTalkerOutputWithPast(ModelOutput):
    loss: Optional[torch.FloatTensor] = None
    past_key_values: Optional[List[torch.FloatTensor]] = None
    hidden_states: Optional[Tuple[torch.FloatTensor]] = None
    attentions: Optional[Tuple[torch.FloatTensor]] = None
    logits: Optional[torch.FloatTensor] = None


class BailingTalkerForConditionalGeneration(PreTrainedModel):
    config_class = BailingTalkerConfig
    base_model_prefix = 'model'

    def __init__(self, config:BailingTalkerConfig):
        super().__init__(config)

        self.config = config
        self.vocab_size = self.config.vocab_size
        self.tokenizer = AutoTokenizer.from_pretrained(self.config._name_or_path)
        self.model_config = Qwen2Config.from_pretrained(self.config._name_or_path)
        self.model = Qwen2ForCausalLM(self.model_config)
        self.model.resize_token_embeddings(self.vocab_size)
        self.thinker_to_talker_proj = nn.Linear(self.config.qa_model_hidden_size, self.model_config.hidden_size)
        self.vp_head = nn.Conv1d(
            self.config.vp_feature_size,
            self.model_config.hidden_size,
            kernel_size=self.config.vp_kernel_size,
            stride=self.config.vp_stride,
            padding=self.config.vp_kernel_size // 2,
        )
        self.s3bpe_tokenizer = S3BpeTokenizer(bpe_model=f"{self.config._name_or_path}/s3_bpe/tokenizer.json", mapping_file=f"{self.config._name_or_path}/s3_bpe/char_mapping.txt")

        self.loss_function = nn.CrossEntropyLoss()

    def get_input_embeddings(self):
        return self.model.get_input_embeddings()

    def encode_audio_segments(
        self,
        inputs_embeds: torch.FloatTensor,
        vp_emb: torch.FloatTensor,
        vp_insert_loc: torch.LongTensor,
        thinker_reply_part: Optional[torch.FloatTensor] = None,
        thinker_reply_length: Optional[List] = None,
        thinker_prefix_insert_loc: Optional[torch.LongTensor] = None
    ):
        vp_emb_encoded = self.vp_head(vp_emb.transpose(-1, -2)).transpose(-1, -2)

        for idx in range(vp_insert_loc.shape[0]):
            inputs_embeds[idx, vp_insert_loc[idx].item():vp_insert_loc[idx].item() + 1, :] = vp_emb_encoded[idx, :, :]

        if thinker_prefix_insert_loc is not None:
            thinker_reply_part = self.thinker_to_talker_proj(thinker_reply_part)
            for idx in range(thinker_prefix_insert_loc.shape[0]):
                real_length = thinker_reply_length[idx]
                inputs_embeds[idx, thinker_prefix_insert_loc[idx].item():thinker_prefix_insert_loc[idx].item() + real_length, :] = thinker_reply_part[idx, :real_length, :]

        return inputs_embeds

    def forward(
        self,
        input_ids: torch.LongTensor = None,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_values: Optional[List[torch.FloatTensor]] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        labels: Optional[dict] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
        text_input_ids: Optional[torch.LongTensor] = None,
        vp_emb: Optional[torch.FloatTensor] = None,
        vp_insert_loc: Optional[torch.LongTensor] = None,
        thinker_reply_part: Optional[torch.FloatTensor] = None,
        thinker_reply_length: Optional[torch.FloatTensor] = None,
        thinker_prefix_insert_loc: Optional[torch.LongTensor] = None,
    ):

        if inputs_embeds is None:
            audio_input_embeds = self.model.get_input_embeddings()(input_ids)
            text_input_embeds = self.model.get_input_embeddings()(text_input_ids)
            inputs_embeds = audio_input_embeds + text_input_embeds
            if past_key_values is None:
                inputs_embeds = self.encode_audio_segments(
                    inputs_embeds, vp_emb, vp_insert_loc, thinker_reply_part=thinker_reply_part,
                    thinker_reply_length=thinker_reply_length, thinker_prefix_insert_loc=thinker_prefix_insert_loc
                )

            if position_ids is None:
                position_ids = (attention_mask.cumsum(-1) - 1).masked_fill_((attention_mask == 0), 1)

        outputs = self.model(
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_values=past_key_values,
            inputs_embeds=inputs_embeds,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        logits = outputs.logits

        loss = None
        if labels is not None:
            loss = self.loss_function(logits.reshape(-1, logits.size(-1)), labels.reshape(-1))

        return BailingTalkerOutputWithPast(
            loss=loss,
            past_key_values=outputs.past_key_values,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
            logits=logits,
        )

    def sample(self, logits, topk=20, filter_value=-float("Inf")):
        logits = logits.reshape(1, -1)  # [1, V]
        indices_to_remove = logits < torch.topk(logits, topk)[0][..., -1, None]
        logits[indices_to_remove] = filter_value
        token_id = torch.multinomial(torch.softmax(logits, dim=-1), num_samples=1).to(torch.long)
        return token_id

    def omni_tts_binary_generation(
        self,
        tts_text,
        vp_emb=None,
        thinker_reply_part=None,
    ):
        # thinker_reply_part: [B, T, d]
        # get text_emb and hidden_states from thinker
        thinker_length = thinker_reply_part.size(1) if thinker_reply_part is not None else 0
        prefix_from_thinker = (
            self.tokenizer.encode("<thinker_prefix>") +
            self.tokenizer.encode("<audio_pad>") * thinker_length +  # placeholder for prefix emb from thinker
            self.tokenizer.encode("</thinker_prefix>")
        )

        prompt = self.tokenizer.encode("<prompt>") + self.tokenizer.encode("</prompt>")

        text_input_part = self.tokenizer.encode(tts_text)
        vp = (
            self.tokenizer.encode("<vp>") +
            self.tokenizer.encode("<audio_pad>") +
            self.tokenizer.encode("</vp>")
        )

        # audio_prefix and text_prefix for first step generation
        talker_text_prefix = (
                prompt +
                prefix_from_thinker +
                vp +
                text_input_part[:1]
        )

        talker_audio_prefix = (
            prompt +
            prefix_from_thinker +
            vp +
            self.tokenizer.encode("<audio_bos>")
        )

        # the rest of input_text
        talker_text_input_part = (
            text_input_part[1:] +
            self.tokenizer.encode("<text_eos>") +
            self.tokenizer.encode("<text_pad>")
        )

        attention_mask = torch.ones(len(talker_audio_prefix)).reshape(1, -1).to(self.device)
        position_ids = (attention_mask.cumsum(-1) - 1).masked_fill_((attention_mask == 0), 1)[:, -1].view(1, -1)

        talker_audio_prefix = torch.tensor(talker_audio_prefix).reshape(1, -1).to(self.device)
        talker_text_prefix = torch.tensor(talker_text_prefix).reshape(1, -1).to(self.device)
        vp_insert_loc = torch.tensor(len(prompt) + len(prefix_from_thinker) + 1, dtype=torch.long).reshape(1, -1)
        vp_emb = vp_emb.unsqueeze(0).to(torch.bfloat16).to(self.device)

        audio_token = self.generate(
            talker_audio_prefix=talker_audio_prefix,
            talker_text_prefix=talker_text_prefix,
            talker_text_input_part=talker_text_input_part,
            position_ids=position_ids,
            vp_emb=vp_emb,
            vp_insert_loc=vp_insert_loc,
            thinker_reply_part=thinker_reply_part,
            thinker_reply_length=torch.tensor([thinker_length]).to(self.device),
            thinker_prefix_insert_loc=torch.tensor([len(prompt) + 1]).to(self.device) if thinker_reply_part is not None else None,
        )

        audio_token = [ele - len(self.tokenizer) for ele in audio_token]
        audio_token = self.s3bpe_tokenizer.decode(audio_token)
        audio_token = torch.tensor([audio_token], dtype=torch.int32)

        return audio_token

    @torch.no_grad()
    def generate(
        self,
        talker_audio_prefix: torch.LongTensor,
        talker_text_prefix: torch.LongTensor,
        talker_text_input_part: List,
        position_ids: Optional[torch.LongTensor] = None,
        vp_emb: Optional[torch.FloatTensor] = None,
        vp_insert_loc: Optional[torch.LongTensor] = None,
        thinker_reply_part: Optional[torch.FloatTensor] = None,
        thinker_reply_length: Optional[torch.FloatTensor] = None,
        thinker_prefix_insert_loc: Optional[torch.LongTensor] = None,
    ):
        result = []
        step = 0
        eos_id = self.tokenizer.encode("<audio_eos>")[0]
        while step < 1000:
            if step == 0:
                talker_audio_input_ids = talker_audio_prefix
                talker_text_input_ids = talker_text_prefix
                attention_mask = torch.ones(talker_audio_input_ids.shape).to(talker_audio_prefix.device)

            else:
                talker_audio_input_ids = next_token
                talker_text_input_ids = torch.tensor(talker_text_input_part[0], dtype=torch.long).reshape(1, -1).to(
                    talker_audio_prefix.device)
                attention_mask = torch.ones(next_token.shape[0], 1).to(talker_audio_prefix.device)
                position_ids += 1
                thinker_prefix_insert_loc = None

                if len(talker_text_input_part) > 1:
                    talker_text_input_part = talker_text_input_part[1:]
            # print(talker_audio_input_ids, self.tokenizer.decode(talker_text_input_ids.tolist()[0]), attention_mask, position_ids)
            outputs = self(
                input_ids=talker_audio_input_ids,
                text_input_ids=talker_text_input_ids,
                thinker_reply_part=thinker_reply_part,
                thinker_reply_length=thinker_reply_length,
                thinker_prefix_insert_loc=thinker_prefix_insert_loc,
                vp_emb=vp_emb,
                vp_insert_loc=vp_insert_loc,
                attention_mask=attention_mask,
                position_ids=position_ids,
                use_cache=True,
                past_key_values=outputs.past_key_values if step > 0 else None
            )
            # 采样
            logits = outputs.logits[:, -1, :]

            next_token = self.sample(logits)
            if next_token.item() == eos_id:
                break
            result.append(next_token.item())
            step += 1

        return result


class AudioDetokenizer:
    def __init__(self, config_path, flow_model_path, hifigan_model_path):
        with open(config_path, 'r') as f:
            configs = load_hyperpyyaml(f)

        self.model = AudioDetokenizerModel(configs['flow'], configs['hift'])
        self.model.load(flow_model_path, hifigan_model_path)
        self.sr = 22050

    def token2wav(self, audio_token, flow_embedding, save_path=None):
        model_input = {"tts_speech_token": audio_token,
                       'flow_embedding': flow_embedding}
        model_output = self.model.inference(**model_input)

        silent_dur = 0.02
        silent_tensor = torch.Tensor([0.0] * int(self.sr * silent_dur))
        model_output['tts_speech'][0][:int(self.sr * silent_dur)] = silent_tensor

        if save_path is not None:
            torchaudio.save(save_path, model_output['tts_speech'], sample_rate=self.sr)
        return model_output['tts_speech']
