import { useState } from 'react';
import { Loader2, Phone, MessageSquare } from 'lucide-react';
import { toast } from 'sonner';
import { client } from '@/api/generated/client.gen';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';

interface PhoneVerificationDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onVerified: () => void;
}

type Step = 'input' | 'verify';

export function PhoneVerificationDialog({ open, onOpenChange, onVerified }: PhoneVerificationDialogProps) {
  const [step, setStep] = useState<Step>('input');
  const [phoneNumber, setPhoneNumber] = useState('');
  const [channel, setChannel] = useState<'sms' | 'call'>('sms');
  const [code, setCode] = useState('');
  const [isSending, setIsSending] = useState(false);
  const [isVerifying, setIsVerifying] = useState(false);

  const resetState = () => {
    setStep('input');
    setPhoneNumber('');
    setChannel('sms');
    setCode('');
    setIsSending(false);
    setIsVerifying(false);
  };

  const handleClose = (isOpen: boolean) => {
    if (!isOpen) {
      resetState();
    }
    onOpenChange(isOpen);
  };

  const handleSendCode = async () => {
    if (!phoneNumber.match(/^\+[1-9]\d{1,14}$/)) {
      toast.error('Enter a valid phone number in E.164 format (e.g. +41791234567)');
      return;
    }

    setIsSending(true);
    try {
      const response = await client.post({
        url: '/api/v1/auth/me/phone/verify',
        body: { phone_number: phoneNumber, channel },
      });

      if (response.error) {
        const detail = (response.error as { detail?: string })?.detail ?? 'Failed to send verification code';
        toast.error(detail);
        return;
      }

      toast.success(`Verification code sent via ${channel === 'sms' ? 'SMS' : 'voice call'}`);
      setStep('verify');
    } catch {
      toast.error('Failed to send verification code');
    } finally {
      setIsSending(false);
    }
  };

  const handleVerifyCode = async () => {
    if (code.length < 4) {
      toast.error('Enter the verification code');
      return;
    }

    setIsVerifying(true);
    try {
      const response = await client.post({
        url: '/api/v1/auth/me/phone/confirm',
        body: { phone_number: phoneNumber, code },
      });

      if (response.error) {
        const detail = (response.error as { detail?: string })?.detail ?? 'Invalid verification code';
        toast.error(detail);
        return;
      }

      toast.success('Phone number verified and saved');
      handleClose(false);
      onVerified();
    } catch {
      toast.error('Failed to verify code');
    } finally {
      setIsVerifying(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={handleClose}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Phone className="h-5 w-5" />
            {step === 'input' ? 'Verify Phone Number' : 'Enter Verification Code'}
          </DialogTitle>
          <DialogDescription>
            {step === 'input'
              ? 'We need to verify that you own this phone number before saving it.'
              : `A verification code has been sent to ${phoneNumber}.`}
          </DialogDescription>
        </DialogHeader>

        {step === 'input' ? (
          <div className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="verify-phone">Phone Number</Label>
              <Input
                id="verify-phone"
                type="tel"
                placeholder="+41791234567"
                value={phoneNumber}
                onChange={(e) => setPhoneNumber(e.target.value)}
                autoFocus
              />
            </div>
            <div className="space-y-2">
              <Label>Delivery Method</Label>
              <Select value={channel} onValueChange={(v) => setChannel(v as 'sms' | 'call')}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="sms">
                    <span className="flex items-center gap-2">
                      <MessageSquare className="h-4 w-4" />
                      SMS
                    </span>
                  </SelectItem>
                  <SelectItem value="call">
                    <span className="flex items-center gap-2">
                      <Phone className="h-4 w-4" />
                      Voice Call
                    </span>
                  </SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
        ) : (
          <div className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="verify-code">Verification Code</Label>
              <Input
                id="verify-code"
                type="text"
                inputMode="numeric"
                placeholder="Enter code"
                value={code}
                onChange={(e) => setCode(e.target.value.replace(/\D/g, '').slice(0, 10))}
                autoFocus
                maxLength={10}
              />
            </div>
            <button
              type="button"
              className="text-sm text-muted-foreground hover:underline"
              onClick={() => { setCode(''); setStep('input'); }}
            >
              Didn&apos;t receive the code? Try again
            </button>
          </div>
        )}

        <DialogFooter>
          <Button variant="outline" onClick={() => handleClose(false)}>
            Cancel
          </Button>
          {step === 'input' ? (
            <Button onClick={handleSendCode} disabled={isSending || !phoneNumber}>
              {isSending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              Send Code
            </Button>
          ) : (
            <Button onClick={handleVerifyCode} disabled={isVerifying || !code}>
              {isVerifying && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              Verify
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
